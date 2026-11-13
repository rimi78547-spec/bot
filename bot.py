import logging
import io
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from telegram import Update, InputFile
from telegram.error import Conflict, NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler, ContextTypes
)
from dotenv import load_dotenv
import ui
from database import db
from generic_panel_monitor import GenericPanelMonitor
from telegram_checker import check_numbers as checker_check_numbers

# Load env variables
load_dotenv()

# Setup basic logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# Thread pool for running blocking DB calls without stalling the event loop
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db_worker")

async def _run_db(func, *args, **kwargs):
    """Run a synchronous DB function in a thread so the event loop stays free."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, partial(func, *args, **kwargs))

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ── USER TRACKING & MODES ───────────────────────────────────────────────────

def _register_user(context, chat_id):
    """Track a user (persisted in DB) for broadcasts. Private chats only.
    Group/channel IDs are negative in Telegram — skip them.
    """
    if not chat_id or int(chat_id) < 0:
        return  # Skip groups, supergroups, channels
    if 'users' not in context.bot_data:
        context.bot_data['users'] = set()
    context.bot_data['users'].add(chat_id)
    db.add_user(chat_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command (Starts in User Mode)."""
    _register_user(context, update.effective_chat.id)
    context.user_data['awaiting_prefix'] = False
    context.user_data['active_prefix'] = None
    context.user_data['number_mode'] = 'random'
    context.user_data['awaiting_admin_broadcast'] = False
    context.user_data['awaiting_admin_country'] = False

    await update.message.reply_text(
        ui.format_welcome_message(),
        reply_markup=ui.get_main_menu_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command."""
    if update.effective_chat.type != "private":
        return
    _register_user(context, update.effective_chat.id)
    help_text = (
        "❓ <b>How to use this bot</b>\n"
        "──────────────────────\n"
        "📱 <b>GET NUMBER</b> — Browse &amp; reserve phone numbers by service and country\n\n"
        "🪵 <b>OTP HISTORY</b> — View the last 20 OTPs received on your numbers\n\n"
        "📌 <b>MY NUMBERS</b> — See all numbers currently assigned to you\n\n"
        "──────────────────────\n"
        "<b>Commands:</b>\n"
        "/start — Restart the bot\n"
        "/help — Show this help message\n"
        "/admin — Switch to Admin Mode (admin only)\n"
        "/user — Switch back to User Mode\n"
        "──────────────────────\n"
        "💬 OTPs are forwarded to you automatically when received."
    )
    await update.message.reply_text(
        help_text,
        parse_mode="HTML",
        reply_markup=ui.get_main_menu_keyboard()
    )

async def user_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch to User Mode."""
    context.user_data['awaiting_prefix'] = False
    context.user_data['active_prefix'] = None
    context.user_data['number_mode'] = 'random'
    context.user_data['awaiting_admin_broadcast'] = False
    context.user_data['awaiting_admin_country'] = False

    await update.message.reply_text(
        "👤 <b>User Mode Active</b>\n\nChoose an option below:",
        parse_mode="HTML",
        reply_markup=ui.get_main_menu_keyboard()
    )

async def admin_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch to Admin Mode (Only for authorized admin)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    context.user_data['awaiting_prefix'] = False
    context.user_data['awaiting_admin_broadcast'] = False
    context.user_data['awaiting_admin_country'] = False

    checker_enabled = context.bot_data.get('checker_enabled', False)
    await update.message.reply_text(
        "🛠 <b>Admin Mode Activated</b>\n\n"
        "Manage services, upload numbers, broadcasts, reports, and checker below:",
        parse_mode="HTML",
        reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
    )


# ── NUMBER ALLOCATION ───────────────────────────────────────────────────────

def _pop_numbers(stock, number_owner, service, country=None, prefix=None,
                 count=5, shuffle=False, chat_id=None, owner_name=None):
    """Reserve `count` unique numbers from the DB stock (persisted)."""
    reserved = db.reserve_numbers(
        service, country=country, prefix=prefix, count=count,
        shuffle=shuffle, chat_id=chat_id, owner_name=owner_name,
    )
    if reserved:
        if chat_id is not None:
            for n in reserved:
                number_owner[str(n).strip()] = chat_id
        reserved_set = set(reserved)
        service_stock = stock.setdefault(service, {})
        for c in list(service_stock.keys()):
            service_stock[c] = [n for n in service_stock.get(c, []) if n not in reserved_set]
    return reserved

_STOCK_LOCK = None  # lazy-init inside event loop

def _get_stock_lock():
    global _STOCK_LOCK
    if _STOCK_LOCK is None:
        _STOCK_LOCK = asyncio.Lock()
    return _STOCK_LOCK

async def _reserve_numbers_with_status(service, country=None, prefix=None, shuffle=False,
                                       chat_id=None, stock=None, number_owner=None,
                                       owner_name=None, checker_enabled=False):
    """Reserve numbers from DB (in thread) then update in-memory cache safely."""
    # Run blocking SQLite write in thread pool — event loop stays free
    reserved = await _run_db(
        db.reserve_numbers,
        service, country=country, prefix=prefix, count=5,
        shuffle=shuffle, chat_id=chat_id, owner_name=owner_name,
    )

    if reserved:
        reserved_set = set(reserved)
        # Protect shared bot_data dicts from concurrent mutation
        async with _get_stock_lock():
            if number_owner is not None and chat_id is not None:
                for n in reserved:
                    s = str(n).strip()
                    clean = s.lstrip('+')
                    number_owner[s] = chat_id
                    number_owner[clean] = chat_id
                    number_owner[f"+{clean}"] = chat_id
            if stock is not None:
                service_stock = stock.setdefault(service, {})
                for c in list(service_stock.keys()):
                    service_stock[c] = [
                        n for n in service_stock.get(c, []) if n not in reserved_set
                    ]

    if reserved and service == "TELEGRAM" and checker_enabled:
        statuses = await checker_check_numbers([str(n).strip() for n in reserved])
    else:
        statuses = None
    return reserved, statuses


# ── TEXT MESSAGE HANDLER ────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all incoming text messages based on current mode and state."""
    # Ignore messages from groups/channels — bot only responds in private chat
    if update.effective_chat and update.effective_chat.type != "private":
        return
    _register_user(context, update.effective_chat.id)
    text = update.message.text.strip()
    user_id = update.effective_user.id
    checker_enabled = context.bot_data.get('checker_enabled', False)

    # ── Mode Switchers ──────────────────────────────────────────────────────
    if "User Mode" in text or text == "/user":
        await user_mode(update, context)
        return

    if text == "/admin":
        await admin_mode(update, context)
        return

    # ── Admin Actions (Only for Admin) ──────────────────────────────────────
    if is_admin(user_id):
        if "Checker System" in text:
            checkers = db.get_all_checkers()
            active_chk = db.get_active_checker()
            active_info = f"🟢 <b>Active:</b> {active_chk['name']} (<code>{active_chk['username']}</code>)" if active_chk else "🔴 <i>No active checker configured.</i>"
            await update.message.reply_text(
                "⚙️ <b>Checker System Management</b>\n\n"
                f"{active_info}\n"
                f"Default URL: <code>http://api.agbots.site:8080/check/</code>\n\n"
                "You can add new Telegram checkers or enable/disable existing ones below:",
                parse_mode="HTML",
                reply_markup=ui.get_admin_checkers_keyboard(checkers)
            )
            return

        if "Checker" in text:
            new_val = not checker_enabled
            context.bot_data['checker_enabled'] = new_val
            db.set_setting('checker_enabled', 'true' if new_val else 'false')
            status_text = (
                "🟢 <b>Checker Mode is now ON</b>\n"
                "Numbers will be verified with the Telegram Checker API and show status dots."
                if new_val else
                "🔴 <b>Checker Mode is now OFF</b>\n"
                "Numbers will be delivered instantly without checking."
            )
            await update.message.reply_text(
                status_text,
                parse_mode="HTML",
                reply_markup=ui.get_admin_reply_keyboard(checker_enabled=new_val)
            )
            return

        if "Manage Panels" in text:
            panels = db.get_all_panels()
            await update.message.reply_text(
                "🌐 <b>Manage Panels</b>\n\n"
                "Here you can add new SMS panels or Start/Stop existing panels.\n"
                "Active panels are shown with 🟢, Stopped panels with 🔴.",
                parse_mode="HTML",
                reply_markup=ui.get_admin_panels_keyboard(panels)
            )
            return

        if "Upload Numbers" in text:
            services = db.get_services()
            await update.message.reply_text(
                "🛠 Select a <b>Service</b> to upload numbers for:",
                parse_mode="HTML",
                reply_markup=ui.get_admin_service_keyboard(services)
            )
            return

        if "Add Service" in text:
            context.user_data['awaiting_add_service'] = True
            await update.message.reply_text(
                "➕ <b>Add New Service</b>\n\n"
                "Send the service in this format:\n"
                "<code>🫆 TELEGRAM</code>\n\n"
                "First part is the emoji+label, last WORD is the service key name.\n"
                "Or type /cancel to abort.",
                parse_mode="HTML"
            )
            return

        if "Remove Service" in text:
            services = db.get_services()
            await update.message.reply_text(
                "🗑 <b>Remove Service</b>\n\nSelect a service to remove:",
                parse_mode="HTML",
                reply_markup=ui.get_admin_remove_service_keyboard(services)
            )
            return

        if "Broadcast Message" in text:
            context.user_data['awaiting_admin_broadcast'] = True
            await update.message.reply_text(
                "📢 Send the <b>message</b> you want to broadcast to all users (or type /cancel):",
                parse_mode="HTML"
            )
            return

        if "Reports" in text:
            await update.message.reply_text(
                "📊 <b>Reports Panel</b>\n\nChoose an option:",
                parse_mode="HTML",
                reply_markup=ui.get_admin_report_keyboard()
            )
            return

        if "Remove Numbers" in text:
            stock_summary = db.get_stock_summary()
            await update.message.reply_text(
                "🗑 <b>Remove Numbers</b>\n\n"
                "Select a service + country to remove <b>ALL</b> numbers (available + assigned):",
                parse_mode="HTML",
                reply_markup=ui.get_admin_remove_stock_keyboard(stock_summary)
            )
            return

        if "Reset DB" in text:
            await update.message.reply_text(
                "⚠️ <b>DANGER: Reset Database</b>\n\n"
                "This will <b>permanently delete</b>:\n"
                "• All numbers (available + assigned)\n"
                "• All OTP records\n"
                "• All processed messages\n\n"
                "⚠️ <b>This action cannot be undone!</b>",
                parse_mode="HTML",
                reply_markup=ui.get_admin_reset_confirm_keyboard()
            )
            return

        # ── Step 1: Panel Name Input ──
        if context.user_data.get('awaiting_panel_name'):
            if text == "/cancel":
                context.user_data['awaiting_panel_name'] = False
                await update.message.reply_text("❌ Cancelled adding panel.")
                return
            context.user_data['awaiting_panel_name'] = False
            panel_name = text.strip()
            context.user_data['temp_panel_name'] = panel_name
            context.user_data['awaiting_panel_url'] = True
            await update.message.reply_text(
                f"✅ Panel Name: <b>{panel_name}</b>\n\n"
                f"🔗 Now send the Panel API URL / Link\n"
                f"(e.g. <code>https://panel.example.com/api/v1/messages</code> or <code>https://api.example.com/cdrs</code>)\n\n"
                f"Or send /cancel to abort:",
                parse_mode="HTML"
            )
            return

        # ── Step 2: Panel URL Input ──
        if context.user_data.get('awaiting_panel_url'):
            if text == "/cancel":
                context.user_data['awaiting_panel_url'] = False
                context.user_data.pop('temp_panel_name', None)
                await update.message.reply_text("❌ Cancelled adding panel.")
                return
            context.user_data['awaiting_panel_url'] = False
            panel_url = text.strip()
            context.user_data['temp_panel_url'] = panel_url
            context.user_data['awaiting_panel_key'] = True
            await update.message.reply_text(
                f"✅ Panel Link: <code>{panel_url}</code>\n\n"
                f"🔑 Now send the API Key / Token for this panel:\n\n"
                f"Or send /cancel to abort:",
                parse_mode="HTML"
            )
            return

        # ── Step 3: Panel API Key Input ──
        if context.user_data.get('awaiting_panel_key'):
            if text == "/cancel":
                context.user_data['awaiting_panel_key'] = False
                context.user_data.pop('temp_panel_name', None)
                context.user_data.pop('temp_panel_url', None)
                await update.message.reply_text("❌ Cancelled adding panel.")
                return
            context.user_data['awaiting_panel_key'] = False
            panel_key = text.strip()
            panel_name = context.user_data.pop('temp_panel_name', 'Custom Panel')
            panel_url = context.user_data.pop('temp_panel_url', '')

            # Save in database
            saved = db.add_panel(panel_name, panel_url, panel_key)
            if saved:
                # Start monitoring task dynamically
                all_panels = db.get_all_panels()
                added_p = next((p for p in all_panels if p['name'] == panel_name), None)
                if added_p:
                    custom_monitors = context.bot_data.setdefault('custom_monitors', {})
                    mon = GenericPanelMonitor(
                        context.application,
                        added_p['id'],
                        added_p['name'],
                        added_p['base_url'],
                        added_p['api_key']
                    )
                    custom_monitors[added_p['id']] = mon
                    await mon.start()

                await update.message.reply_text(
                    f"🎉 <b>Panel Added & Started Successfully!</b>\n\n"
                    f"📛 Name: <b>{panel_name}</b>\n"
                    f"🔗 Link: <code>{panel_url}</code>\n"
                    f"🔑 Key: <code>{panel_key[:6]}...{panel_key[-4:] if len(panel_key) > 10 else ''}</code>\n"
                    f"🟢 Status: <b>Active (Running)</b>",
                    parse_mode="HTML",
                    reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
                )
            else:
                await update.message.reply_text(
                    f"⚠️ Failed to add panel. A panel with name <b>{panel_name}</b> might already exist.",
                    parse_mode="HTML"
                )
            return

        # ── Checker Step 1: Username (auth) Input ──
        if context.user_data.get('awaiting_checker_username'):
            if text == "/cancel":
                context.user_data['awaiting_checker_username'] = False
                await update.message.reply_text("❌ Cancelled adding checker.")
                return
            context.user_data['awaiting_checker_username'] = False
            username = text.strip()
            context.user_data['temp_checker_username'] = username
            context.user_data['awaiting_checker_key'] = True
            await update.message.reply_text(
                f"✅ Username: <code>{username}</code>\n\n"
                f"🔑 Now send your Checker <b>API Key</b> (Step 2/2):\n\n"
                f"Or send /cancel to abort:",
                parse_mode="HTML"
            )
            return

        # ── Checker Step 2: API Key Input ──
        if context.user_data.get('awaiting_checker_key'):
            if text == "/cancel":
                context.user_data['awaiting_checker_key'] = False
                context.user_data.pop('temp_checker_username', None)
                await update.message.reply_text("❌ Cancelled adding checker.")
                return
            context.user_data['awaiting_checker_key'] = False
            api_key = text.strip()
            username = context.user_data.pop('temp_checker_username', '')
            checker_name = f"Agbots ({username})"
            default_url = "http://api.agbots.site:8080/check/"

            # Save in database
            saved = db.add_checker(checker_name, username, api_key, url=default_url)
            if saved:
                # Also ensure checker_enabled is ON
                context.bot_data['checker_enabled'] = True
                db.set_setting('checker_enabled', 'true')
                await update.message.reply_text(
                    f"🎉 <b>Checker Added & Activated Successfully!</b>\n\n"
                    f"👤 Username: <code>{username}</code>\n"
                    f"🔑 API Key: <code>{api_key[:4]}...{api_key[-4:] if len(api_key) > 8 else ''}</code>\n"
                    f"🔗 URL: <code>{default_url}</code>\n"
                    f"🟢 Status: <b>Active (ON)</b>",
                    parse_mode="HTML",
                    reply_markup=ui.get_admin_reply_keyboard(checker_enabled=True)
                )
            else:
                await update.message.reply_text(
                    f"⚠️ Failed to add checker. Please try again.",
                    parse_mode="HTML"
                )
            return

        if context.user_data.get('awaiting_admin_broadcast'):
            if text == "/cancel":
                context.user_data['awaiting_admin_broadcast'] = False
                await update.message.reply_text(
                    "❌ Broadcast cancelled.",
                    reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
                )
                return

            context.user_data['awaiting_admin_broadcast'] = False
            # Only private users (positive IDs); groups have negative IDs
            users = [u for u in db.get_users() if int(u) > 0] or [update.effective_chat.id]
            success = 0
            for uid in users:
                try:
                    await context.bot.send_message(chat_id=uid, text=text)
                    success += 1
                except Exception as e:
                    logger.error(f"Broadcast failed for {uid}: {e}")

            await update.message.reply_text(
                f"📢 Broadcast sent to <b>{success}</b> users.",
                parse_mode="HTML",
                reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
            )
            return

        if context.user_data.get('awaiting_add_service'):
            if text == "/cancel":
                context.user_data['awaiting_add_service'] = False
                await update.message.reply_text(
                    "❌ Cancelled.",
                    reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
                )
                return
            # Parse: e.g. "✈️ TELEGRAM" → label="✈️ TELEGRAM", name="TELEGRAM"
            parts = text.strip().split()
            if len(parts) < 1:
                await update.message.reply_text("❌ Invalid format. Send like: ✈️ TELEGRAM")
                return
            name = parts[-1].upper()  # last word = service key
            label = text.strip()       # full text = display label
            services = db.get_services()
            # Check duplicate
            if any(s["name"] == name for s in services):
                await update.message.reply_text(
                    f"⚠️ Service <b>{name}</b> already exists.",
                    parse_mode="HTML"
                )
                context.user_data['awaiting_add_service'] = False
                return
            services.append({"label": label, "name": name})
            db.save_services(services)
            context.user_data['awaiting_add_service'] = False
            await update.message.reply_text(
                f"✅ Service <b>{label}</b> added successfully!",
                parse_mode="HTML",
                reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
            )
            return

        if context.user_data.get('awaiting_admin_country'):
            context.user_data['awaiting_admin_country'] = False
            country = text.upper()
            service = context.user_data.get('admin_service', 'TELEGRAM')
            context.user_data['admin_country'] = country
            await update.message.reply_text(
                f"✅ Service: <b>{service}</b>\n"
                f"✅ Country: <b>{country}</b>\n\n"
                f"📂 Now send the <b>.txt</b>, <b>.csv</b>, or <b>.xlsx</b> file containing phone numbers:",
                parse_mode="HTML"
            )
            return

    # ── User Prefix Filter State ────────────────────────────────────────────
    if context.user_data.get('awaiting_prefix'):
        context.user_data['awaiting_prefix'] = False
        prefix = text.strip()
        service = context.user_data.get('selected_service', 'TELEGRAM')
        country = context.user_data.get('selected_country')
        stock = context.bot_data.setdefault('stock', {})
        number_owner = context.bot_data.setdefault('number_owner', {})

        context.user_data['number_mode'] = 'prefix'
        context.user_data['active_prefix'] = prefix

        # 1. Clean up prompt message & user's input message
        prompt_msg_id = context.user_data.pop('prefix_prompt_msg_id', None)
        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass

        # 2. Release the user's old active numbers (keeps owner_chat_id for late OTP routing)
        released = await _run_db(
            db.release_user_numbers,
            update.effective_chat.id, service, country
        )
        if released:
            logger.info(f"Released {released} old numbers for user {update.effective_chat.id} before prefix assignment")
            # Refresh in-memory stock cache after releasing
            context.bot_data['stock'] = await _run_db(db.get_stock)
            context.bot_data['number_owner'] = await _run_db(db.get_number_owner_map)

        # 3. Reserve new numbers with prefix (DB retains ownership history via replaced status)
        reserved, statuses = await _reserve_numbers_with_status(
            service, country=country, prefix=prefix, shuffle=False,
            chat_id=update.effective_chat.id,
            stock=stock, number_owner=number_owner,
            owner_name=update.effective_user.full_name,
            checker_enabled=checker_enabled
        )

        if not reserved:
            context.user_data['number_mode'] = 'random'
            context.user_data['active_prefix'] = None
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ No numbers found with prefix <code>{prefix}</code> for <b>{service}</b>.",
                parse_mode="HTML"
            )
        else:
            # 3. Delete the previous active numbers message from chat so old numbers disappear
            last_numbers_msg_id = context.user_data.pop('last_numbers_msg_id', None)
            if last_numbers_msg_id:
                try:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=last_numbers_msg_id)
                except Exception:
                    pass

            # Send standard telegram message with inline keyboard
            new_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=ui.format_available_numbers_message(service, mode="PREFIX", prefix=prefix),
                reply_markup=ui.get_available_numbers_keyboard(reserved, country=country, statuses=statuses),
                parse_mode="HTML"
            )
            context.user_data['last_numbers_msg_id'] = new_msg.message_id
            context.user_data['active_numbers'] = reserved
        return

    # ── User Actions ────────────────────────────────────────────────────────
    if "GET NUMBER" in text:
        services = db.get_services()
        await update.message.reply_text(
            "📍 Select a service:",
            reply_markup=ui.get_service_selection_keyboard(services)
        )
        return

    elif "OTP HISTORY" in text:
        chat_id = update.effective_chat.id
        today_count, total_count, history = db.get_user_otp_stats_and_history(chat_id, limit=20)
        
        header = (
            "📜 <b>YOUR OTP HISTORY & STATS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 <b>Today (BD Time 6AM-6AM):</b> {today_count}\n"
            f"📈 <b>Total Received:</b> {total_count}"
        )

        if not history:
            await update.message.reply_text(
                f"{header}\n\n"
                "📭 <i>No OTPs received yet. OTPs will appear here once your numbers receive them.</i>",
                parse_mode="HTML"
            )
        else:
            lines = [header, "──────────────────────"]
            for number, service, otp, created_at in history:
                flag = ui.get_country_flag(phone_number=number)
                ts = str(created_at)[:16].replace("T", " ")
                lines.append(
                    f"📞 {flag} <code>{number}</code>\n"
                    f"📱 {service}  🔑 <b>{otp}</b>  🕐 {ts}"
                )
            await update.message.reply_text(
                "\n".join(lines[:2]) + "\n\n" + "\n──────────\n".join(lines[2:]),
                parse_mode="HTML"
            )
        return

    elif "MY NUMBERS" in text:
        chat_id = update.effective_chat.id
        numbers = db.get_user_numbers(chat_id)
        if not numbers:
            await update.message.reply_text(
                "📭 <b>You have no assigned numbers yet.</b>\n\n"
                "Use 📱 <b>GET NUMBER</b> to reserve numbers.",
                parse_mode="HTML"
            )
        else:
            lines = [f"📌 <b>Your Numbers ({len(numbers)})</b>\n──────────────────────"]
            for number, service, country, assigned_at in numbers:
                flag = ui.get_country_flag(phone_number=number)
                ts = str(assigned_at)[:10] if assigned_at else "N/A"
                lines.append(f"{flag} <code>{number}</code> — {service} ({country}) · {ts}")
            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML"
            )
        return


# ── DOCUMENT / FILE UPLOAD HANDLER ──────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file upload (.txt / .csv / .xlsx) for adding stock or Lamix assign/unassign."""
    # Only process documents in private chat
    if update.effective_chat and update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return

    checker_enabled = context.bot_data.get('checker_enabled', False)
    document = update.message.document
    fname = (document.file_name or "").lower() if document else ""

    SUPPORTED = (".txt", ".csv", ".xlsx", ".xls")
    if not document or not any(fname.endswith(ext) for ext in SUPPORTED):
        await update.message.reply_text(
            "❌ Please send a valid file.\n"
            "Supported formats: <b>.txt</b>, <b>.csv</b>, <b>.xlsx</b>",
            parse_mode="HTML"
        )
        return

    file = await document.get_file()
    content = await file.download_as_bytearray()
    numbers = []

    if fname.endswith(".xlsx") or fname.endswith(".xls"):
        try:
            import io as _io
            import openpyxl
            wb = openpyxl.load_workbook(_io.BytesIO(bytes(content)), read_only=True, data_only=True)
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    for cell in row:
                        if cell is not None:
                            val = str(cell).strip()
                            if val and val not in ("None", ""):
                                numbers.append(val)
            wb.close()
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to read Excel file: <code>{e}</code>", parse_mode="HTML")
            return
    else:
        lines = content.decode('utf-8', errors='ignore').strip().splitlines()
        for line in lines:
            for part in line.split(","):
                val = part.strip()
                if val:
                    numbers.append(val)

    # Keep only values that look like phone numbers (digits / + signs, min 5 chars)
    numbers = [n for n in numbers if len(n) >= 5 and all(c in "0123456789+ -" for c in n)]

    if not numbers:
        await update.message.reply_text("❌ The file is empty or contains no valid numbers.")
        return

    # ── Normal stock upload flow ─────────────────────────────────────────────
    service = context.user_data.get('admin_service', 'TELEGRAM')
    country = context.user_data.get('admin_country', 'GERMANY')

    added = db.add_numbers(service, country, numbers)

    context.bot_data['stock'] = db.get_stock()
    context.bot_data['number_owner'] = db.get_number_owner_map()

    dupes = len(numbers) - added
    await update.message.reply_text(
        f"✅ <b>{added} numbers</b> added for <b>{service} - {country}</b>"
        f"{f' (<code>{dupes}</code> duplicates skipped)' if dupes else ''}.\n"
        f"📢 Broadcasting to all users...",
        parse_mode="HTML",
        reply_markup=ui.get_admin_reply_keyboard(checker_enabled=checker_enabled)
    )

    # Broadcast new stock
    SERVICE_ICONS = {"TELEGRAM": "✈️", "WHATSAPP": "🟢", "IMO": "💬", "VK": "🧊"}
    icon = SERVICE_ICONS.get(service, "📱")
    flag = ui.get_country_flag(country)

    msg_text = ui.format_broadcast_message(service, country, icon=icon, flag=flag, count=added)
    reply_markup = ui.get_broadcast_keyboard()

    # Only private users (positive IDs); groups have negative IDs
    users = [u for u in db.get_users() if int(u) > 0] or [update.effective_chat.id]
    success = 0
    for user_id in users:
        for attempt in range(3):  # Retry up to 3 times
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=msg_text,
                    reply_markup=reply_markup
                )
                success += 1
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.0)
                else:
                    logger.error(f"Broadcast failed for {user_id} after 3 attempts: {e}")
        await asyncio.sleep(0.05)  # Avoid Telegram flood limits


# ── CALLBACK QUERY HANDLER ──────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries from inline keyboards."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    checker_enabled = context.bot_data.get('checker_enabled', False)

    if query.message and query.message.chat.type == 'private':
        _register_user(context, query.message.chat.id)

    # ── User Service Selection ──────────────────────────────────────────────
    if data.startswith("service_"):
        service_name = data.split("_", 1)[1].upper()
        context.user_data['selected_service'] = service_name
        # Clear prefix when a new service is picked
        context.user_data['number_mode'] = 'random'
        context.user_data['active_prefix'] = None
        context.user_data['awaiting_prefix'] = False
        
        # Get active stock for this service from DB
        stock = db.get_stock().get(service_name, {})
        context.bot_data['stock'] = db.get_stock()
        
        active_countries = []
        for c, nums in stock.items():
            if nums:
                flag = ui.get_country_flag(c)
                active_countries.append((f"{flag} {c.upper()} ({len(nums)})", c))

        if not active_countries:
            await query.edit_message_text(
                f"⚠️ No numbers available for <b>{service_name}</b> yet.\n"
                f"Please choose another service or try again later:",
                reply_markup=ui.get_service_selection_keyboard(),
                parse_mode="HTML"
            )
            return

        await query.edit_message_text(
            f"🌍 Select a country for <b>{service_name}</b>:",
            reply_markup=ui.get_country_selection_keyboard(service_name, active_countries),
            parse_mode="HTML"
        )
        return

    elif data.startswith("country_"):
        country = data.split("_", 1)[1]
        service = context.user_data.get('selected_service', 'TELEGRAM')
        stock = context.bot_data.setdefault('stock', {})
        number_owner = context.bot_data.setdefault('number_owner', {})
        context.user_data['selected_country'] = country
        context.user_data['number_mode'] = 'random'
        context.user_data['active_prefix'] = None
        context.user_data['awaiting_prefix'] = False

        reserved, statuses = await _reserve_numbers_with_status(
            service, country=country, prefix=None, shuffle=True,
            chat_id=update.effective_chat.id,
            stock=stock, number_owner=number_owner,
            owner_name=update.effective_user.full_name,
            checker_enabled=checker_enabled
        )
        if not reserved:
            await query.edit_message_text("⚠️ Numbers for this country have run out. Try another.")
            return

        msg = await query.edit_message_text(
            ui.format_available_numbers_message(service, mode="RANDOM", prefix=None),
            reply_markup=ui.get_available_numbers_keyboard(reserved, country=country, statuses=statuses),
            parse_mode="HTML"
        )
        context.user_data['last_numbers_msg_id'] = msg.message_id
        context.user_data['active_numbers'] = reserved

    elif data == "change_service":
        context.user_data['number_mode'] = 'random'
        context.user_data['active_prefix'] = None
        context.user_data['awaiting_prefix'] = False
        context.user_data['selected_country'] = None
        services = db.get_services()
        await query.edit_message_text(
            "📍 Select a service:",
            reply_markup=ui.get_service_selection_keyboard(services)
        )

    elif data == "get_number_broadcast":
        context.user_data['number_mode'] = 'random'
        context.user_data['active_prefix'] = None
        context.user_data['awaiting_prefix'] = False
        services = db.get_services()
        await query.message.reply_text(
            "📍 Select a service:",
            reply_markup=ui.get_service_selection_keyboard(services)
        )

    elif data == "prefix_filter":
        service = context.user_data.get('selected_service', 'the service')
        context.user_data['awaiting_prefix'] = True
        context.user_data['last_numbers_msg_id'] = query.message.message_id
        prompt_msg = await query.message.reply_text(
            f"🎯 Send prefix code for <b>{service}</b> (e.g. <code>21355</code>):",
            parse_mode="HTML"
        )
        context.user_data['prefix_prompt_msg_id'] = prompt_msg.message_id

    elif data in ["next_numbers", "random_number"]:
        service = context.user_data.get('selected_service', 'TELEGRAM')
        country = context.user_data.get('selected_country')
        stock = context.bot_data.setdefault('stock', {})
        number_owner = context.bot_data.setdefault('number_owner', {})

        if data == "random_number":
            # Switch mode to RANDOM and clear active_prefix
            context.user_data['number_mode'] = 'random'
            context.user_data['active_prefix'] = None
            prefix = None
            shuffle = True
            mode = "RANDOM"
        else:
            # data == "next_numbers"
            # If currently in prefix mode and prefix is set, continue with that prefix
            if context.user_data.get('number_mode') == 'prefix' and context.user_data.get('active_prefix'):
                prefix = context.user_data.get('active_prefix')
                shuffle = False
                mode = "PREFIX"
            else:
                # In random mode, 'next' gives random numbers until a prefix is set again
                context.user_data['number_mode'] = 'random'
                context.user_data['active_prefix'] = None
                prefix = None
                shuffle = True
                mode = "RANDOM"

        reserved, statuses = await _reserve_numbers_with_status(
            service, country=country, prefix=prefix, shuffle=shuffle,
            chat_id=update.effective_chat.id,
            stock=stock, number_owner=number_owner,
            owner_name=update.effective_user.full_name,
            checker_enabled=checker_enabled
        )

        if reserved:
            text = ui.format_available_numbers_message(service, mode=mode, prefix=prefix)
            reply_markup = ui.get_available_numbers_keyboard(reserved, country=country, statuses=statuses)
            try:
                msg = await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
                context.user_data['last_numbers_msg_id'] = msg.message_id
            except Exception:
                pass
            context.user_data['active_numbers'] = reserved
            context.user_data['last_numbers_msg_id'] = query.message.message_id
        else:
            if prefix:
                text = f"⚠️ No more numbers available with prefix <code>{prefix}</code> for <b>{service}</b>."
            else:
                text = f"⚠️ No more numbers available for <b>{service}</b>. Please try another service or country."
            reply_markup = ui.get_available_numbers_keyboard([], country=country)
            msg = await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            context.user_data['last_numbers_msg_id'] = msg.message_id

    # ── Admin Callbacks ─────────────────────────────────────────────────────
    elif data.startswith("admin_svc_"):
        if not is_admin(user_id):
            return
        service = data.replace("admin_svc_", "")
        context.user_data['admin_service'] = service
        context.user_data['awaiting_admin_country'] = True
        await query.edit_message_text(
            f"✅ Service: <b>{service}</b>\n\n"
            f"✏️ Now type the <b>Country name</b> (e.g. <code>GERMANY</code> or <code>SRI LANKA</code>):",
            parse_mode="HTML"
        )

    elif data == "admin_cancel":
        await query.edit_message_text("❌ Action cancelled.")

    elif data.startswith("admin_rm_confirm_"):
        if not is_admin(user_id):
            return
        parts = data.replace("admin_rm_confirm_", "").split("_", 1)
        service = parts[0]
        country = parts[1] if len(parts) > 1 else ""
        removed = db.remove_numbers(service, country)
        context.bot_data['stock'] = db.get_stock()
        context.bot_data['number_owner'] = db.get_number_owner_map()
        flag = ui.get_country_flag(country)
        await query.edit_message_text(
            f"✅ <b>{removed}</b> numbers removed for "
            f"<b>{service}</b> – {flag} <b>{country}</b>.\n"
            f"(All available + assigned numbers deleted)",
            parse_mode="HTML"
        )

    elif data == "admin_reset_confirm":
        if not is_admin(user_id):
            return
        db.reset_db()
        context.bot_data['stock'] = {}
        context.bot_data['number_owner'] = {}
        await query.edit_message_text(
            "✅ <b>Database has been reset!</b>\n\n"
            "All numbers, OTPs, and processed messages have been deleted.",
            parse_mode="HTML"
        )

    elif data.startswith("admin_rm_svc_"):
        if not is_admin(user_id):
            return
        svc_name = data.replace("admin_rm_svc_", "").upper()
        services = db.get_services()
        new_services = [s for s in services if s["name"] != svc_name]
        if len(new_services) == len(services):
            await query.edit_message_text(f"⚠️ Service <b>{svc_name}</b> not found.", parse_mode="HTML")
            return
        db.save_services(new_services)
        await query.edit_message_text(
            f"✅ Service <b>{svc_name}</b> removed successfully!",
            parse_mode="HTML"
        )

    elif data.startswith("admin_rm_"):
        if not is_admin(user_id):
            return
        # data = admin_rm_SERVICE_COUNTRY
        parts = data.replace("admin_rm_", "").split("_", 1)
        service = parts[0]
        country = parts[1] if len(parts) > 1 else ""
        # Get count for confirmation
        stock = db.get_stock()
        count = len(stock.get(service, {}).get(country, []))
        flag = ui.get_country_flag(country)
        await query.edit_message_text(
            f"⚠️ <b>Are you sure?</b>\n\n"
            f"This will permanently remove <b>{count}</b> available numbers for:\n"
            f"📦 Service: <b>{service}</b>\n"
            f"🌍 Country: {flag} <b>{country}</b>\n\n"
            f"⚠️ Assigned numbers will NOT be affected.",
            parse_mode="HTML",
            reply_markup=ui.get_admin_remove_confirm_keyboard(service, country, count)
        )

    elif data == "admin_report_back":
        if not is_admin(user_id):
            return
        await query.edit_message_text(
            "📊 <b>Reports Panel</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=ui.get_admin_report_keyboard()
        )

    # ── Custom Panels Callback Handlers ──
    elif data == "panel_add_start":
        if not is_admin(user_id):
            return
        context.user_data['awaiting_panel_name'] = True
        await query.message.reply_text(
            "➕ <b>Add New Panel (Step 1/3)</b>\n\n"
            "Please send the <b>Name</b> for this panel (e.g. <code>Panel 7</code> or <code>FastSMS</code>):\n\n"
            "Or send /cancel to abort.",
            parse_mode="HTML"
        )

    elif data.startswith("panel_toggle_"):
        if not is_admin(user_id):
            return
        p_id = int(data.replace("panel_toggle_", ""))
        new_status = db.toggle_panel_status(p_id)
        custom_monitors = context.bot_data.setdefault('custom_monitors', {})

        if new_status is True:
            # Re-start or start monitor
            all_panels = db.get_all_panels()
            p_data = next((p for p in all_panels if p['id'] == p_id), None)
            if p_data:
                if p_id in custom_monitors:
                    await custom_monitors[p_id].stop()
                mon = GenericPanelMonitor(
                    context.application,
                    p_data['id'],
                    p_data['name'],
                    p_data['base_url'],
                    p_data['api_key']
                )
                custom_monitors[p_id] = mon
                await mon.start()
        elif new_status is False:
            # Stop monitor
            if p_id in custom_monitors:
                await custom_monitors[p_id].stop()

        panels = db.get_all_panels()
        await query.edit_message_text(
            "🌐 <b>Manage Panels</b>\n\n"
            "Panel status updated.\n"
            "Active panels are shown with 🟢, Stopped panels with 🔴.",
            parse_mode="HTML",
            reply_markup=ui.get_admin_panels_keyboard(panels)
        )

    elif data.startswith("panel_delete_"):
        if not is_admin(user_id):
            return
        p_id = int(data.replace("panel_delete_", ""))
        custom_monitors = context.bot_data.setdefault('custom_monitors', {})
        if p_id in custom_monitors:
            await custom_monitors[p_id].stop()
            custom_monitors.pop(p_id, None)
        db.delete_panel(p_id)

        panels = db.get_all_panels()
        await query.edit_message_text(
            "🌐 <b>Manage Panels</b>\n\n"
            "🗑 Panel deleted.\n"
            "Active panels are shown with 🟢, Stopped panels with 🔴.",
            parse_mode="HTML",
            reply_markup=ui.get_admin_panels_keyboard(panels)
        )

    # ── Custom Checkers Callback Handlers ──
    elif data == "checker_add_start":
        if not is_admin(user_id):
            return
        context.user_data['awaiting_checker_username'] = True
        await query.message.reply_text(
            "⚙️ <b>Add New Checker (Step 1/2)</b>\n\n"
            "👤 Please send your Checker <b>Username (auth)</b>:\n"
            "URL: <code>http://api.agbots.site:8080/check/</code>\n\n"
            "Or send /cancel to abort.",
            parse_mode="HTML"
        )

    elif data.startswith("checker_toggle_"):
        if not is_admin(user_id):
            return
        c_id = int(data.replace("checker_toggle_", ""))
        db.toggle_checker_status(c_id)
        checkers = db.get_all_checkers()
        active_chk = db.get_active_checker()
        active_info = f"🟢 <b>Active:</b> {active_chk['name']} (<code>{active_chk['username']}</code>)" if active_chk else "🔴 <i>No active checker configured.</i>"
        await query.edit_message_text(
            "⚙️ <b>Checker System Management</b>\n\n"
            f"{active_info}\n"
            f"Default URL: <code>http://api.agbots.site:8080/check/</code>\n\n"
            "Checker status updated below:",
            parse_mode="HTML",
            reply_markup=ui.get_admin_checkers_keyboard(checkers)
        )

    elif data.startswith("checker_delete_"):
        if not is_admin(user_id):
            return
        c_id = int(data.replace("checker_delete_", ""))
        db.delete_checker(c_id)
        checkers = db.get_all_checkers()
        active_chk = db.get_active_checker()
        active_info = f"🟢 <b>Active:</b> {active_chk['name']} (<code>{active_chk['username']}</code>)" if active_chk else "🔴 <i>No active checker configured.</i>"
        await query.edit_message_text(
            "⚙️ <b>Checker System Management</b>\n\n"
            f"{active_info}\n"
            "🗑 Checker deleted successfully:\n\n"
            f"Default URL: <code>http://api.agbots.site:8080/check/</code>",
            parse_mode="HTML",
            reply_markup=ui.get_admin_checkers_keyboard(checkers)
        )

    elif data == "report_summary":
        if not is_admin(user_id):
            return
        rows = db.otp_summary("today")
        text = "📊 <b>OTP Report (Today)</b>\n\n"
        if not rows:
            text += "No OTPs received today yet."
        else:
            for name, cid, nums, otps in rows:
                text += (
                    f"👤 <b>{name}</b> (<code>{cid}</code>)\n"
                    f"📱 Numbers with OTP: {nums}\n"
                    f"🔑 Total OTPs: {otps}\n──────────\n"
                )
            text += (
                f"\n👥 Users: {len(rows)}\n"
                f"📱 Numbers with OTP: {sum(r[2] for r in rows)}\n"
                f"🔑 Total OTPs: {sum(r[3] for r in rows)}"
            )
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=ui.get_admin_report_keyboard())

    elif data in ["report_with_today", "report_without_today", "report_with_all", "report_without_all"]:
        if not is_admin(user_id):
            return
        if data == "report_with_today":
            nums, filename = db.numbers_with_otp("today"), "numbers_with_otp_today.txt"
        elif data == "report_without_today":
            nums, filename = db.numbers_without_otp("today"), "numbers_without_otp_today.txt"
        elif data == "report_with_all":
            nums, filename = db.numbers_with_otp("all"), "numbers_with_otp_all.txt"
        else:
            nums, filename = db.numbers_without_otp("all"), "numbers_without_otp_all.txt"

        if not nums:
            await query.message.reply_text("⚠️ No numbers found for this report.")
            return

        f = io.BytesIO(("\n".join(str(n) for n in nums) + "\n").encode("utf-8"))
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=InputFile(f, filename=filename),
            caption=f"📄 {len(nums)} numbers"
        )


# ── MAIN APPLICATION ────────────────────────────────────────────────────────

def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN is missing in the .env file.")
        return

    # Larger HTTP connection pool for handling many concurrent users
    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        http_version="1.1",
    )

    application = (
        Application.builder()
        .token(bot_token)
        .request(request)
        .concurrent_updates(True)   # Each update handled in parallel — no user waits for another
        .build()
    )

    # Restore persistent state
    application.bot_data['stock'] = db.get_stock()
    application.bot_data['number_owner'] = db.get_number_owner_map()
    application.bot_data['users'] = set(db.get_users())
    saved_checker = db.get_setting("checker_enabled", "false")
    application.bot_data['checker_enabled'] = (saved_checker.lower() == "true")

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_mode))
    application.add_handler(CommandHandler("user", user_mode))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Global error handler: suppress 409 Conflict traceback spam (caused by duplicate instances)
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.warning(
                "⚠️  HTTP 409 Conflict: Another bot instance is still running. "
                "Stop all old instances and restart. Error: %s", err
            )
        elif isinstance(err, NetworkError):
            logger.warning("Network error (will retry automatically): %s", err)
        else:
            logger.error("Unhandled exception in update handler:", exc_info=err)

    application.add_error_handler(error_handler)

    async def post_init(app):
        # Start any active dynamic panels added by Admin
        custom_monitors = app.bot_data.setdefault('custom_monitors', {})
        for p in db.get_all_panels():
            if p.get('is_active'):
                try:
                    mon = GenericPanelMonitor(
                        app,
                        p['id'],
                        p['name'],
                        p['base_url'],
                        p['api_key']
                    )
                    custom_monitors[p['id']] = mon
                    await mon.start()
                except Exception as e:
                    logger.error(f"Failed to start dynamic panel monitor [{p.get('name')}]: {e}")

    application.post_init = post_init

    print("Bot is running...")
    application.run_polling(
        bootstrap_retries=5,
        poll_interval=0,           # No artificial delay — get updates as fast as possible
        drop_pending_updates=True, # Ignore stale updates from before bot started
    )

if __name__ == "__main__":
    main()
