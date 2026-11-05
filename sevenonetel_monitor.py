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

ARABIC_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')


def extract_otp(sms_content: str, service: str = '') -> str:
    text = (sms_content or '').strip()
    if not text:
        return text
    text = text.translate(ARABIC_DIGITS)
    is_whatsapp = 'whatsapp' in (service or '').lower() or 'whatsapp' in text.lower() or 'واتساب' in text
    m_wa = re.search(r'\b(\d{3})[-\s](\d{3})\b', text)
    if is_whatsapp and m_wa:
        return f'{m_wa.group(1)}-{m_wa.group(2)}'
    kw_pattern = (r'(?:code|codigo|رمز|كود|код|otp|pin|verification|verif' + r'ication|passcode|secret|security|bestätigungscode)[^\d\n]{0,35}?(\d{3}[-\s]\d{3}|\d{4,8})')
    m_kw = re.search(kw_pattern, text, re.IGNORECASE)
    if m_kw:
        raw_code = m_kw.group(1)
        if is_whatsapp and re.match(r'^\d{3}[-\s]\d{3}$', raw_code):
            c = re.sub(r'\D', '', raw_code)
            return f'{c[:3]}-{c[3:]}'
        clean = re.sub(r'\D', '', raw_code)
        if 4 <= len(clean) <= 8:
            return clean
    if is_whatsapp:
        m_6 = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
        if m_6:
            c = m_6.group(1)
            return f'{c[:3]}-{c[3:]}'
    if m_wa:
        return f'{m_wa.group(1)}{m_wa.group(2)}'
    m_num = re.search(r'(?<!\d)(\d{4,8})(?!\d)', text)
    if m_num:
        return m_num.group(1)
    m_3 = re.search(r'(?<!\d)(\d{3})(?!\d)', text)
    if m_3:
        return m_3.group(1)
    return text[:10] if len(text) > 10 else text


class SevenOneTelMonitor:
    PANEL_NAME = 'SevenOneTel'
    PANEL_TAG  = 'sevenonetel'

    def __init__(self, application):
        self.application = application
        self.is_running = False
        self._task = None
        self.group_id = os.getenv('FORWARD_GROUP_ID')
        self.api_key = (os.getenv('SEVENONETEL_API_KEY') or '7d531aeb2b4940b591fa3635a34ef793').strip()
        raw_base = (os.getenv('SEVENONETEL_BASE_URL') or 'https://www.sevenonetel.com/api/v1/client/cdrs').strip().rstrip('/')
        self.base_url = raw_base
        try:
            self.poll_interval = float(os.getenv('SEVENONETEL_POLL_INTERVAL', '3.0'))
        except ValueError:
            self.poll_interval = 3.0
        self._session = None
        self.sent_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sevenonetel_already_sent.json')
        self.sent_messages = self._load_sent_messages()
        self._unsaved_hashes = False
        self._user_name_cache = {}
        self._error_count = 0
        self._cycle_count = 0
        self._first_cycle = True  # On first startup, only forward today's (UTC+0) OTPs

    def _refresh_env(self):
        load_dotenv(override=True)
        if not self.api_key:
            self.api_key = (os.getenv('SEVENONETEL_API_KEY') or '7d531aeb2b4940b591fa3635a34ef793').strip()
        if not self.group_id:
            self.group_id = os.getenv('FORWARD_GROUP_ID')

    def _load_sent_messages(self) -> set:
        if os.path.exists(self.sent_json_path):
            try:
                with open(self.sent_json_path, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
            except Exception as e:
                logger.error(f'Error loading {self.sent_json_path}: {e}')
        return set()

    def _save_sent_message(self, msg_hash: str):
        self.sent_messages.add(msg_hash)
        self._unsaved_hashes = True

    def _flush_sent_messages(self):
        if not getattr(self, '_unsaved_hashes', False):
            return
        self._unsaved_hashes = False
        try:
            hashes = list(self.sent_messages)
            if len(hashes) > 5000:
                hashes = hashes[-5000:]
                self.sent_messages = set(hashes)
            with open(self.sent_json_path, 'w', encoding='utf-8') as f:
                json.dump(hashes, f, indent=2)
        except Exception as e:
            logger.error(f'Error saving to {self.sent_json_path}: {e}')

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def start(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info(f'{self.PANEL_NAME} API Monitor (Panel 6) started successfully.')

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
        logger.info(f'{self.PANEL_NAME} API Monitor (Panel 6) stopped.')

    def _find_owner(self, pn: str):
        clean_pn = str(pn).strip().lstrip('+')
        owner_map = self.application.bot_data.get('number_owner', {})
        owner_id = (owner_map.get(str(pn).strip()) or owner_map.get(clean_pn) or owner_map.get(f'+{clean_pn}'))
        owner_name = 'N/A'
        if not owner_id:
            try:
                db_owner_id, db_owner_name = db.get_number_owner_info(pn)
                if db_owner_id:
                    owner_id = db_owner_id
                    if db_owner_name:
                        owner_name = db_owner_name
            except Exception as e:
                logger.error(f'Error looking up owner from db: {e}')
        if not owner_id:
            for k, v in owner_map.items():
                k_clean = str(k).strip().lstrip('+')
                if k_clean == clean_pn or (len(clean_pn) >= 7 and len(k_clean) >= 7 and (clean_pn.endswith(k_clean) or k_clean.endswith(clean_pn))):
                    owner_id = v
                    break
        if not owner_id:
            try:
                conn = db._connect()
                suffix = clean_pn[-8:] if len(clean_pn) >= 8 else clean_pn
                row = conn.execute('SELECT owner_chat_id, owner_name FROM numbers WHERE (number LIKE ? OR number LIKE ?) AND owner_chat_id IS NOT NULL LIMIT 1', (f'%{suffix}', f'%{clean_pn}%')).fetchone()
                if row:
                    owner_id = row[0]
                    if row[1]:
                        owner_name = row[1]
            except Exception as e:
                logger.error(f'Error in flexible DB owner lookup: {e}')
        return owner_id, owner_name

    async def _run_loop(self):
        logger.info(f'Starting {self.PANEL_NAME} API monitoring loop on {self.base_url} (Poll interval: {self.poll_interval}s)')
        while self.is_running:
            try:
                self._cycle_count += 1
                self._refresh_env()
                session = await self._get_session()
                headers = {'Authorization': f'Bearer {self.api_key}', 'X-API-Key': self.api_key, 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) SevenBot/1.0'}
                params  = {'api_key': self.api_key, 'limit': '100', 'page': '1'}
                async with session.get(self.base_url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status == 429:
                        retry_after = response.headers.get('Retry-After', '5')
                        try:
                            wait_time = max(float(retry_after), 3.0)
                        except ValueError:
                            wait_time = 5.0
                        logger.warning(f'{self.PANEL_NAME} rate limit hit (HTTP 429). Retrying after {wait_time}s...')
                        await asyncio.sleep(wait_time)
                        continue
                    if response.status == 401:
                        logger.error(f'{self.PANEL_NAME}: HTTP 401 Unauthorized - check SEVENONETEL_API_KEY.')
                        self._error_count += 1
                        await asyncio.sleep(min(60, 5 * self._error_count))
                        continue
                    if response.status != 200:
                        logger.warning(f'{self.PANEL_NAME} API Error: HTTP {response.status}')
                        self._error_count += 1
                        await asyncio.sleep(min(30, 2.5 * self._error_count))
                        continue
                    body = await response.text()
                try:
                    data = json.loads(body)
                except Exception as e:
                    logger.error(f'{self.PANEL_NAME} failed to parse JSON: {e} | Body: {body[:300]}')
                    self._error_count += 1
                    await asyncio.sleep(min(30, 2.5 * self._error_count))
                    continue
                self._error_count = 0
                records = (data.get('data') or data.get('cdrs') or data.get('records') or data.get('result') or (data if isinstance(data, list) else []))
                if not isinstance(records, list):
                    records = []
                today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                is_first = self._first_cycle
                self._first_cycle = False
                new_found = 0
                for rec in reversed(records):
                    if not isinstance(rec, dict):
                        continue
                    pn = str(rec.get('num') or rec.get('number') or rec.get('phone') or rec.get('destination') or rec.get('did_number') or rec.get('callee') or rec.get('to') or '').strip()
                    service = str(rec.get('cli') or rec.get('source') or rec.get('sender') or rec.get('caller') or rec.get('from') or rec.get('service') or 'SMS').strip()
                    sms_content = str(rec.get('message') or rec.get('body') or rec.get('content') or rec.get('text') or rec.get('sms') or '').strip()
                    if not pn or not sms_content:
                        continue
                    if pn in ['0', '0,0,0,0'] or pn.startswith('0,0'):
                        continue
                    if len(sms_content) < 3 or sms_content.lower() in ['wait', 'waiting', 'none']:
                        continue
                    rec_id   = str(rec.get('id') or rec.get('cdr_id') or '').strip()
                    rec_time = str(rec.get('dt') or rec.get('created_at') or rec.get('timestamp') or rec.get('date') or '').strip()
                    unique_seed = f'{self.PANEL_TAG}_{pn}_{rec_id or rec_time}_{sms_content}'
                    msg_hash = hashlib.md5(unique_seed.encode()).hexdigest()
                    # On first startup: skip (mark seen) records not from today UTC+0
                    if is_first and rec_time and not rec_time.startswith(today_utc):
                        self._save_sent_message(msg_hash)
                        continue
                    if msg_hash not in self.sent_messages:
                        if not await db.is_msg_processed(msg_hash):
                            logger.info(f'New {self.PANEL_NAME} SMS detected for {pn} ({service}): {sms_content}')
                            new_found += 1
                            self.sent_messages.add(msg_hash)
                            prefix = _ui.extract_country_prefix(pn)
                            otp = extract_otp(sms_content, service)
                            owner_id, owner_name = self._find_owner(pn)
                            if owner_id:
                                if owner_id in self._user_name_cache:
                                    owner_name = self._user_name_cache[owner_id]
                                elif owner_name and owner_name != 'N/A':
                                    self._user_name_cache[owner_id] = owner_name
                                else:
                                    try:
                                        chat = await self.application.bot.get_chat(owner_id)
                                        owner_name = (chat.first_name or '') + ((' ' + chat.last_name) if chat.last_name else '')
                                        owner_name = owner_name.strip() or 'N/A'
                                        self._user_name_cache[owner_id] = owner_name
                                    except Exception as e:
                                        logger.error(f'Failed to resolve owner name {owner_id}: {e}')
                            if self.group_id:
                                otp_keyboard = _ui.get_otp_keyboard(otp, prefix)
                                card_img = None
                                try:
                                    card_img = _ui.generate_otp_card(pn, service, otp, prefix, sms_content=sms_content)
                                except Exception as e_card:
                                    logger.error(f"Failed to generate OTP card image: {e_card}")

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
                                                reply_markup=otp_keyboard
                                            )
                                        else:
                                            group_text = _ui.format_otp_group_message(pn, service, otp, prefix, owner_name, sms_content=sms_content)
                                            await self.application.bot.send_message(chat_id=self.group_id, text=group_text, parse_mode='HTML', reply_markup=otp_keyboard)
                                        break
                                    except Exception as e:
                                        err_str = str(e)
                                        if 'Flood control' in err_str or 'RetryAfter' in err_str or 'retry after' in err_str.lower():
                                            m_flood = re.search(r'(\d+)', err_str)
                                            wait_flood = int(m_flood.group(1)) + 2 if m_flood else 30
                                            logger.warning(f'{self.PANEL_NAME}: Flood control hit. Waiting {wait_flood}s (attempt {_attempt+1}/3)...')
                                            await asyncio.sleep(wait_flood)
                                        else:
                                            logger.error(f'Failed to forward {self.PANEL_NAME} SMS to group: {e}')
                                            break
                            if owner_id:
                                try:
                                    private_text = _ui.format_otp_private_message(pn, service, otp, prefix)
                                    private_keyboard = _ui.get_private_otp_keyboard(otp)
                                    await self.application.bot.send_message(chat_id=owner_id, text=private_text, parse_mode='HTML', reply_markup=private_keyboard)
                                    logger.info(f'Successfully forwarded {self.PANEL_NAME} OTP to owner {owner_id} for number {pn}')
                                except Exception as e:
                                    logger.error(f'Failed to forward {self.PANEL_NAME} OTP to owner {owner_id}: {e}')
                            try:
                                await db.mark_msg_processed(msg_hash)
                                db.log_otp(pn, service, otp, msg_hash)
                            except Exception as e:
                                logger.error(f'Failed to persist {self.PANEL_NAME} message {msg_hash}: {e}')
                            self._save_sent_message(msg_hash)
                            await asyncio.sleep(0.3)
                        else:
                            self._save_sent_message(msg_hash)
                self._flush_sent_messages()
                if new_found:
                    logger.info(f'{self.PANEL_NAME} API Monitor forwarded {new_found} new message(s) this cycle.')
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'Error in {self.PANEL_NAME} Monitor loop: {e}', exc_info=True)
                self._error_count += 1
            await asyncio.sleep(self.poll_interval)
