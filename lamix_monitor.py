import asyncio
import logging
import hashlib
import os
import io
import json
import re
import aiohttp
from datetime import datetime, timezone
from dotenv import load_dotenv
from database import db
import ui as _ui

load_dotenv()

logger = logging.getLogger(__name__)

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def extract_otp(sms_content: str, service: str = "") -> str:
    """Extract the actual OTP code from the SMS body across multiple languages.

    - WhatsApp codes are formatted as 680-755.
    - Telegram and all other services show clean numeric digits (e.g. 75579).
    - Multi-language support: English, French, Arabic, Spanish, Russian, German, etc.
    - Never dumps full multi-line SMS text into the copy button.
    """
    text = (sms_content or "").strip()
    if not text:
        return text

    # Convert Eastern Arabic numerals (٠-٩) to standard (0-9)
    text = text.translate(ARABIC_DIGITS)

    is_whatsapp = "whatsapp" in (service or "").lower() or "whatsapp" in text.lower() or "واتساب" in text

    # 1. WhatsApp specific: 3 digits, hyphen/space, 3 digits -> 680-755
    m_wa = re.search(r'\b(\d{3})[-\s](\d{3})\b', text)
    if is_whatsapp and m_wa:
        return f"{m_wa.group(1)}-{m_wa.group(2)}"

    # 2. Multi-language keywords
    kw_pattern = (
        r'(?:code|código|codigo|رمز|كود|код|otp|pin|verification|vérification|verificação|'
        r'пароль|passcode|secret|security|sécurité|seguridad|bestätigungscode)'
        r'[^\d\n]{0,35}?(\d{3}[-\s]\d{3}|\d{4,8})'
    )
    m_kw = re.search(kw_pattern, text, re.IGNORECASE)
    if m_kw:
        raw_code = m_kw.group(1)
        if is_whatsapp and re.match(r'^\d{3}[-\s]\d{3}$', raw_code):
            c = re.sub(r'\D', '', raw_code)
            return f"{c[:3]}-{c[3:]}"
        clean = re.sub(r'\D', '', raw_code)
        if 4 <= len(clean) <= 8:
            return clean

    # 3. If WhatsApp message has a 6-digit continuous number, format as 680-755
    if is_whatsapp:
        m_6 = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
        if m_6:
            c = m_6.group(1)
            return f"{c[:3]}-{c[3:]}"

    # 4. If split 3-3 format in other services (e.g. Telegram 849-204)
    if m_wa:
        return f"{m_wa.group(1)}{m_wa.group(2)}"

    # 5. Standalone 4 to 8 digits
    m_num = re.search(r'(?<!\d)(\d{4,8})(?!\d)', text)
    if m_num:
        return m_num.group(1)

    # 6. Fallback: 3 digits if no longer digits exist
    m_3 = re.search(r'(?<!\d)(\d{3})(?!\d)', text)
    if m_3:
        return m_3.group(1)

    # If completely non-numeric, return trimmed text
    return text[:10] if len(text) > 10 else text


class LamixMonitor:
    """Monitor for Panel 3 (Lamix REST API: https://panel.lamix.org/api/v1/messages).

    Calls the API, deduplicates received SMS messages, extracts the OTP,
    and forwards formatted alert messages directly to the designated Telegram group.
    """

    def __init__(self, application):
        self.application = application
        self.is_running = False
        self._task = None

        self.group_id = os.getenv("FORWARD_GROUP_ID")
        self.api_token = os.getenv("LAMIX_API_TOKEN", "").strip()
        
        raw_base = (
            os.getenv("LAMIX_BASE_URL") or "https://panel.lamix.org/api/v1"
        ).strip().rstrip("/")
        # If full endpoint was given in BASE_URL, strip it down
        if raw_base.endswith("/messages") or raw_base.endswith("/cdrs"):
            raw_base = raw_base.rsplit("/", 1)[0]
        self.base_url = raw_base

        # Lamix rate limit: 1 req/s per endpoint, 40 req/60s per endpoint.
        # 1.5s poll interval = 40 req/min — using the full rate limit allowance.
        try:
            self.poll_interval = float(os.getenv("LAMIX_POLL_INTERVAL", "1.5"))
        except ValueError:
            self.poll_interval = 1.5

        self._session = None
        self.sent_json_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "lamix_already_sent.json"
        )
        self.sent_messages = self._load_sent_messages()
        self._unsaved_hashes = False
        self._user_name_cache = {}
        self._error_count = 0
        self._cycle_count = 0
        self._first_cycle = True  # On first startup, only forward today's (UTC+0) OTPs

    def _refresh_env(self):
        """Reload credentials from .env if needed."""
        load_dotenv(override=True)
        self.api_token = os.getenv("LAMIX_API_TOKEN", "").strip()
        if not self.group_id:
            self.group_id = os.getenv("FORWARD_GROUP_ID")

    def _load_sent_messages(self) -> set:
        if os.path.exists(self.sent_json_path):
            try:
                with open(self.sent_json_path, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception as e:
                logger.error(f"Error loading {self.sent_json_path}: {e}")
        return set()

    def _save_sent_message(self, msg_hash: str):
        self.sent_messages.add(msg_hash)
        self._unsaved_hashes = True

    def _flush_sent_messages(self):
        if not self._unsaved_hashes:
            return
        self._unsaved_hashes = False
        try:
            hashes = list(self.sent_messages)
            if len(hashes) > 5000:
                hashes = hashes[-5000:]
                self.sent_messages = set(hashes)
            with open(self.sent_json_path, "w", encoding="utf-8") as f:
                json.dump(hashes, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving to {self.sent_json_path}: {e}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def start(self):
        if not self.api_token:
            logger.info("Lamix API Monitor (Panel 3) is disabled (no API token). Skipping.")
            return
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Lamix API Monitor (Panel 3) started successfully.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("Lamix API Monitor (Panel 3) stopped.")

    async def _run_loop(self):
        if not self.api_token:
            logger.info("Lamix API Monitor disabled (no token). Exiting loop.")
            self.is_running = False
            return
        logger.info(f"Starting Lamix API monitoring loop on {self.base_url} (Poll interval: {self.poll_interval}s)")
        # Small startup delay — Lamix allows 1 req/s, so stagger SevenOneTel and Lamix starts
        await asyncio.sleep(2.0)

        while self.is_running:
            try:
                if not self.api_token:
                    await asyncio.sleep(30)
                    self._refresh_env()
                    continue
                self._cycle_count += 1
                session = await self._get_session()

                # Call GET /api/v1/messages?limit=25 (fast payload transfer)
                url = f"{self.base_url}/messages"
                params = {
                    "token": self.api_token,
                    "limit": "25",
                }
                headers = {
                    "Authorization": f"Bearer {self.api_token}",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SevenBot/1.0",
                }

                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    # Handle rate limiting (429) — strictly obey Retry-After header
                    # Lamix docs: 1 req/s hard limit, returns Retry-After on 429
                    if response.status == 429:
                        retry_after_hdr = response.headers.get("Retry-After", "")
                        try:
                            # Use the server-specified wait time (minimum 1.1s to respect 1 req/s rule)
                            wait_time = max(float(retry_after_hdr), 1.1)
                        except (ValueError, TypeError):
                            wait_time = 5.0
                        logger.warning(f"Lamix rate limit (HTTP 429). Waiting {wait_time:.1f}s as instructed by Retry-After header...")
                        await asyncio.sleep(wait_time)
                        continue

                    if response.status != 200:
                        logger.warning(f"Lamix API Error: HTTP {response.status}")
                        self._error_count += 1
                        await asyncio.sleep(min(30, 2.5 * self._error_count))
                        continue

                    body = await response.text()

                try:
                    data = json.loads(body)
                except Exception as e:
                    logger.error(f"Lamix API failed to parse JSON: {e} | Body: {body[:300]}")
                    self._error_count += 1
                    await asyncio.sleep(min(30, 2.5 * self._error_count))
                    continue

                # Reset consecutive error counter on success
                self._error_count = 0

                records = data.get("records", [])
                if not isinstance(records, list):
                    records = []

                today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                is_first = self._first_cycle
                self._first_cycle = False

                new_found = 0
                # Process records in chronological order (oldest first, so newest comes last)
                for rec in reversed(records):
                    if not isinstance(rec, dict):
                        continue

                    # Extract phone number
                    pn = str(
                        rec.get("number")
                        or rec.get("phone")
                        or rec.get("destination")
                        or rec.get("to")
                        or ""
                    ).strip()

                    # Extract service / CLI
                    service = str(
                        rec.get("cli")
                        or rec.get("service")
                        or rec.get("sender")
                        or rec.get("from")
                        or "SMS"
                    ).strip()

                    # Extract SMS content (Lamix uses 'content' field)
                    sms_content = str(
                        rec.get("content")
                        or rec.get("message")
                        or rec.get("text")
                        or rec.get("sms")
                        or rec.get("body")
                        or ""
                    ).strip()

                    if not pn or not sms_content or pn in ["0", "0,0,0,0"] or pn.startswith("0,0"):
                        continue

                    if len(sms_content) < 3 or sms_content.lower() in ["wait", "waiting", "none"]:
                        continue

                    # Unique identifier / timestamp (Lamix uses 'time' field)
                    rec_id = str(rec.get("id") or rec.get("uuid") or rec.get("cdrId") or "").strip()
                    rec_time = str(rec.get("time") or rec.get("receivedAt") or rec.get("createdAt") or rec.get("date") or "").strip()

                    # Unique hash to prevent duplicate forwards
                    unique_seed = f"lamix_{pn}_{rec_id or rec_time}_{sms_content}"
                    msg_hash = hashlib.md5(unique_seed.encode()).hexdigest()

                    # On first startup: skip (mark seen) records not from today UTC+0
                    if is_first and rec_time and not rec_time.startswith(today_utc):
                        self._save_sent_message(msg_hash)
                        continue

                    if msg_hash not in self.sent_messages:
                        if not await db.is_msg_processed(msg_hash):
                            logger.info(f"New Lamix SMS detected for {pn} ({service}): {sms_content}")
                            new_found += 1

                            # Reserve in memory immediately
                            self.sent_messages.add(msg_hash)

                            prefix = _ui.extract_country_prefix(pn)
                            otp = extract_otp(sms_content, service)

                            # Resolve user name and owner_id
                            clean_pn = str(pn).strip().lstrip('+')
                            owner_map = self.application.bot_data.get('number_owner', {})
                            owner_id = (
                                owner_map.get(str(pn).strip())
                                or owner_map.get(clean_pn)
                                or owner_map.get(f"+{clean_pn}")
                            )
                            owner_name = "N/A"

                            if not owner_id:
                                try:
                                    db_owner_id, db_owner_name = db.get_number_owner_info(pn)
                                    if db_owner_id:
                                        owner_id = db_owner_id
                                        if db_owner_name:
                                            owner_name = db_owner_name
                                except Exception as e:
                                    logger.error(f"Error looking up owner from db: {e}")

                            if owner_id:
                                if owner_id in self._user_name_cache:
                                    owner_name = self._user_name_cache[owner_id]
                                elif owner_name and owner_name != "N/A":
                                    self._user_name_cache[owner_id] = owner_name
                                else:
                                    try:
                                        chat = await self.application.bot.get_chat(owner_id)
                                        owner_name = (
                                            chat.first_name or ""
                                        ) + ((" " + chat.last_name) if chat.last_name else "")
                                        owner_name = owner_name.strip() or "N/A"
                                        self._user_name_cache[owner_id] = owner_name
                                    except Exception as e:
                                        logger.error(f"Failed to resolve owner name {owner_id}: {e}")

                            # Format group keyboard and image card
                            otp_keyboard = _ui.get_otp_keyboard(otp, prefix)
                            card_img = None
                            try:
                                card_img = _ui.generate_otp_card(pn, service, otp, prefix, sms_content=sms_content)
                            except Exception as e_card:
                                logger.error(f"Failed to generate Lamix OTP card image: {e_card}")

                            # Forward SMS directly to the Telegram group (with RetryAfter / flood control handling)
                            session = await self._get_session()
                            bot_token = self.application.bot.token
                            for _attempt in range(3):
                                try:
                                    sent_rich = await _ui.send_rich_otp_group_message(
                                        session, bot_token, self.group_id, pn, service, otp, prefix, owner_name, sms_content=sms_content
                                    )
                                    if sent_rich:
                                        break
                                    if card_img:
                                        await self.application.bot.send_photo(
                                            chat_id=self.group_id,
                                            photo=io.BytesIO(card_img),
                                            caption=".",
                                            reply_markup=otp_keyboard,
                                        )
                                    else:
                                        group_text = _ui.format_otp_group_message(
                                            pn, service, otp, prefix, owner_name, sms_content=sms_content
                                        )
                                        await self.application.bot.send_message(
                                            chat_id=self.group_id,
                                            text=group_text,
                                            parse_mode="HTML",
                                            reply_markup=otp_keyboard,
                                        )
                                    break
                                except Exception as e:
                                    err_str = str(e)
                                    if "Flood control" in err_str or "RetryAfter" in err_str or "retry after" in err_str.lower():
                                        import re as _re
                                        m = _re.search(r'(\d+)', err_str)
                                        wait_flood = int(m.group(1)) + 2 if m else 30
                                        logger.warning(f"Lamix: Flood control hit. Waiting {wait_flood}s before retry (attempt {_attempt+1}/3)...")
                                        await asyncio.sleep(wait_flood)
                                    else:
                                        logger.error(f"Failed to forward Lamix SMS to group: {e}")
                                        break

                            # Forward to this number's owner privately in bot (if known)
                            if owner_id:
                                try:
                                    private_text = _ui.format_otp_private_message(
                                        pn, service, otp, prefix
                                    )
                                    private_keyboard = _ui.get_private_otp_keyboard(otp)
                                    await self.application.bot.send_message(
                                        chat_id=owner_id,
                                        text=private_text,
                                        parse_mode="HTML",
                                        reply_markup=private_keyboard
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to forward Lamix OTP to owner {owner_id}: {e}")

                            # Mark as processed in database
                            try:
                                await db.mark_msg_processed(msg_hash)
                                db.log_otp(pn, service, otp, msg_hash)
                            except Exception as e:
                                logger.error(f"Failed to persist Lamix message {msg_hash}: {e}")

                            self._save_sent_message(msg_hash)

                            # Snappy delivery without excessive blocking
                            await asyncio.sleep(0.3)
                        else:
                            self._save_sent_message(msg_hash)

                self._flush_sent_messages()
                if new_found:
                    logger.info(f"Lamix API Monitor forwarded {new_found} new message(s) this cycle.")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Lamix Monitor loop: {e}", exc_info=True)
                self._error_count += 1

            # Sleep between polling cycles respecting rate limit
            await asyncio.sleep(self.poll_interval)
