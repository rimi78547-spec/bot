import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
import aiohttp
from dotenv import load_dotenv

from database import db
import ui as _ui

logger = logging.getLogger(__name__)

ARABIC_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')


def extract_otp(sms_content: str, service: str = "") -> str:
    """Extract OTP code across multi-languages."""
    text = (sms_content or "").strip()
    if not text:
        return text
    text = text.translate(ARABIC_DIGITS)
    is_whatsapp = "whatsapp" in (service or "").lower() or "whatsapp" in text.lower() or "واتساب" in text
    if is_whatsapp:
        m_dash = re.search(r"(?<!\d)(\d{3})[- ](\d{3})(?!\d)", text)
        if m_dash:
            return f"{m_dash.group(1)}-{m_dash.group(2)}"
        m_6 = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if m_6:
            code = m_6.group(1)
            return f"{code[:3]}-{code[3:]}"

    patterns = [
        r"(?:code|otp|verification|pin|password|c[oó]digo|passcode|code de confirmation|رمز|код)[\s:=-]+([0-9]{4,8})",
        r"(?<!\d)(\d{6})(?!\d)",
        r"(?<!\d)(\d{4,5})(?!\d)",
        r"(?<!\d)(\d{7,8})(?!\d)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)

    m_3 = re.search(r"(?<!\d)(\d{3})(?!\d)", text)
    if m_3:
        return m_3.group(1)
    return text[:10] if len(text) > 10 else text


class GenericPanelMonitor:
    """Dynamic Monitor for custom API panels added by Admin.
    Supports endpoints returning list of SMS records or object with list (data, messages, records, cdrs, etc.).
    """

    def __init__(self, application, panel_id: int, name: str, base_url: str, api_key: str):
        self.application = application
        self.panel_id = panel_id
        self.name = name.strip()
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.is_running = False
        self._task = None
        self.group_id = os.getenv("FORWARD_GROUP_ID")
        self.poll_interval = 2.5
        self._session = None

        safe_tag = re.sub(r"[^a-zA-Z0-9_]", "_", self.name.lower())
        self.sent_json_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"panel_{self.panel_id}_{safe_tag}_sent.json"
        )
        self.sent_messages = self._load_sent_messages()
        self._unsaved_hashes = False
        self._user_name_cache = {}
        self._error_count = 0
        self._first_cycle = True

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
            logger.error(f"Error saving {self.sent_json_path}: {e}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25)
            )
        return self._session

    async def start(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info(f"Custom Panel [{self.name}] monitor started.")

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
        self._flush_sent_messages()
        logger.info(f"Custom Panel [{self.name}] monitor stopped.")

    def _find_owner(self, pn: str):
        clean_pn = str(pn).strip().lstrip("+")
        owner_map = self.application.bot_data.get("number_owner", {})
        owner_id = (
            owner_map.get(str(pn).strip())
            or owner_map.get(clean_pn)
            or owner_map.get(f"+{clean_pn}")
        )
        owner_name = "N/A"
        return owner_id, owner_name

    async def _run_loop(self):
        logger.info(f"Starting {self.name} API monitor loop on {self.base_url}")
        while self.is_running:
            try:
                session = await self._get_session()
                # Prepare headers & params covering common token/api_key standards
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-API-Key": self.api_key,
                    "token": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SevenBot/1.0",
                }
                params = {
                    "api_key": self.api_key,
                    "token": self.api_key,
                    "limit": "50",
                }

                url = self.base_url
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After", "5")
                        try:
                            wait_time = max(float(retry_after), 3.0)
                        except ValueError:
                            wait_time = 5.0
                        await asyncio.sleep(wait_time)
                        continue

                    if resp.status != 200:
                        logger.warning(f"Panel [{self.name}] API HTTP {resp.status}")
                        self._error_count += 1
                        await asyncio.sleep(min(30, 3 * self._error_count))
                        continue

                    body = await resp.text()

                try:
                    data = json.loads(body)
                except Exception as e:
                    logger.error(f"Panel [{self.name}] parse JSON failed: {e}")
                    self._error_count += 1
                    await asyncio.sleep(min(30, 3 * self._error_count))
                    continue

                self._error_count = 0
                records = (
                    data.get("data")
                    or data.get("messages")
                    or data.get("cdrs")
                    or data.get("records")
                    or data.get("result")
                    or (data if isinstance(data, list) else [])
                )
                if not isinstance(records, list):
                    records = []

                today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                is_first = self._first_cycle
                self._first_cycle = False

                for rec in reversed(records):
                    if not isinstance(rec, dict):
                        continue
                    pn = str(
                        rec.get("num")
                        or rec.get("number")
                        or rec.get("phone")
                        or rec.get("destination")
                        or rec.get("did_number")
                        or rec.get("to")
                        or ""
                    ).strip()

                    service = str(
                        rec.get("cli")
                        or rec.get("source")
                        or rec.get("sender")
                        or rec.get("service")
                        or rec.get("from")
                        or "SMS"
                    ).strip()

                    sms_content = str(
                        rec.get("message")
                        or rec.get("body")
                        or rec.get("content")
                        or rec.get("text")
                        or rec.get("sms")
                        or ""
                    ).strip()

                    if not pn or not sms_content:
                        continue
                    if len(sms_content) < 3 or sms_content.lower() in ["wait", "waiting", "none"]:
                        continue

                    rec_id = str(rec.get("id") or rec.get("cdr_id") or "").strip()
                    rec_time = str(rec.get("dt") or rec.get("created_at") or rec.get("timestamp") or rec.get("date") or "").strip()

                    unique_seed = f"{self.panel_id}_{pn}_{rec_id or rec_time}_{sms_content}"
                    msg_hash = hashlib.md5(unique_seed.encode()).hexdigest()

                    if is_first and rec_time and not rec_time.startswith(today_utc):
                        self._save_sent_message(msg_hash)
                        continue

                    if msg_hash not in self.sent_messages:
                        if not await db.is_msg_processed(msg_hash):
                            logger.info(f"New SMS on panel [{self.name}] for {pn} ({service}): {sms_content}")
                            self.sent_messages.add(msg_hash)
                            prefix = _ui.extract_country_prefix(pn)
                            otp = extract_otp(sms_content, service)
                            owner_id, owner_name = self._find_owner(pn)
                            if owner_id:
                                if owner_id in self._user_name_cache:
                                    owner_name = self._user_name_cache[owner_id]
                                else:
                                    try:
                                        chat = await self.application.bot.get_chat(owner_id)
                                        owner_name = chat.full_name or chat.username or "N/A"
                                        self._user_name_cache[owner_id] = owner_name
                                    except Exception:
                                        pass

                            # Send to Forward Group (Rich format)
                            if self.group_id:
                                bot_token = self.application.bot.token
                                for _ in range(3):
                                    try:
                                        sent = await _ui.send_rich_otp_group_message(
                                            session, bot_token, self.group_id, pn, service, otp, prefix, owner_name, sms_content=sms_content
                                        )
                                        if sent:
                                            break
                                        group_text = _ui.format_otp_group_message(pn, service, otp, prefix, owner_name, sms_content=sms_content)
                                        await self.application.bot.send_message(
                                            chat_id=self.group_id, text=group_text, parse_mode="HTML", reply_markup=_ui.get_otp_keyboard(otp)
                                        )
                                        break
                                    except Exception as e:
                                        if "RetryAfter" in str(e):
                                            await asyncio.sleep(10)
                                        else:
                                            logger.error(f"Forward SMS group error: {e}")
                                            break

                            # Send to private owner if known
                            if owner_id:
                                try:
                                    priv_text = _ui.format_otp_private_message(pn, service, otp, prefix)
                                    priv_kb = _ui.get_private_otp_keyboard(otp)
                                    await self.application.bot.send_message(
                                        chat_id=owner_id, text=priv_text, parse_mode="HTML", reply_markup=priv_kb
                                    )
                                except Exception as e:
                                    logger.error(f"Private OTP send error: {e}")

                            try:
                                await db.mark_msg_processed(msg_hash)
                                db.log_otp(pn, service, otp, msg_hash)
                            except Exception as e:
                                logger.error(f"DB persist error: {e}")

                            self._save_sent_message(msg_hash)
                            await asyncio.sleep(0.3)
                        else:
                            self._save_sent_message(msg_hash)

                self._flush_sent_messages()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Panel [{self.name}] loop: {e}", exc_info=True)
                self._error_count += 1
            await asyncio.sleep(self.poll_interval)

