import os
import random
import threading
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime

logger = logging.getLogger(__name__)

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "https://ovhqorkmwipxpzbxzywh.supabase.co").strip().rstrip("/")
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "sb_publishable_Odvhc7PLvccbY9pleKOQmg_xM6Wo-mo").strip()


class Database:
    def __init__(self, base_url: str = SUPABASE_URL, api_key: str = SUPABASE_KEY):
        self.base_url = base_url
        self.api_key = api_key
        self.rest_url = f"{self.base_url}/rest/v1"
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler())
        
        # High speed in-memory cache for ultra-fast bot response
        self._cache_lock = threading.Lock()
        self._stock_cache = {}          # {service: {country: [numbers]}}
        self._owner_map = {}            # {number: chat_id}
        self._owner_info = {}           # {number: (chat_id, name)}
        self._users_cache = set()       # {chat_id}
        self._processed_hashes = set()  # {msg_hash}
        self._services_cache = None     # [services]
        self._settings_cache = {}       # {key: val}
        self._panels_cache = None       # [panels]

        # Initial fast preload from Cloud Supabase
        self.reload_cache()

    def _headers(self, prefer=None) -> dict:
        h = {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def _req(self, endpoint: str, method: str = "GET", params: dict = None, json_data: any = None, prefer: str = None):
        url = f"{self.rest_url}/{endpoint}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        
        data_bytes = None
        if json_data is not None:
            data_bytes = json.dumps(json_data).encode("utf-8")

        req = urllib.request.Request(url, data=data_bytes, headers=self._headers(prefer), method=method)
        try:
            with self._opener.open(req, timeout=10) as res:
                body = res.read().decode("utf-8")
                if not body:
                    return None
                try:
                    return json.loads(body)
                except Exception:
                    return body
        except Exception as e:
            logger.error(f"Supabase REST error [{method} {endpoint}]: {e}")
            return None

    def reload_cache(self):
        """Preload active state from Supabase into memory for ultra-fast instant lookups."""
        try:
            # 1. Available Numbers (Stock)
            res = self._req("numbers", params={"status": "eq.available", "select": "service,country,number"})
            new_stock = {}
            if isinstance(res, list):
                for r in res:
                    s = (r.get("service") or "").upper()
                    c = (r.get("country") or "").upper()
                    n = str(r.get("number")).strip()
                    if s and c and n:
                        new_stock.setdefault(s, {}).setdefault(c, []).append(n)

            # 2. Number Ownership Map
            res_assigned = self._req("numbers", params={"owner_chat_id": "not.is.null", "select": "number,owner_chat_id,owner_name"})
            new_owner_map = {}
            new_owner_info = {}
            if isinstance(res_assigned, list):
                for r in res_assigned:
                    cid = r.get("owner_chat_id")
                    name = r.get("owner_name")
                    n_str = str(r.get("number")).strip()
                    clean = n_str.lstrip("+")
                    new_owner_map[n_str] = cid
                    new_owner_map[clean] = cid
                    new_owner_map[f"+{clean}"] = cid
                    info = (cid, name)
                    new_owner_info[n_str] = info
                    new_owner_info[clean] = info
                    new_owner_info[f"+{clean}"] = info

            # 3. Users
            res_users = self._req("users", params={"select": "chat_id"})
            new_users = set()
            if isinstance(res_users, list):
                for u in res_users:
                    if u.get("chat_id"):
                        new_users.add(u["chat_id"])

            with self._cache_lock:
                self._stock_cache = new_stock
                self._owner_map = new_owner_map
                self._owner_info = new_owner_info
                self._users_cache = new_users

            logger.info("Ultra-fast cache reloaded successfully from Supabase!")
        except Exception as e:
            logger.error(f"Error during cache preload: {e}")

    # ── Processed messages (Monitor) ────────────────────────────────────────

    async def is_msg_processed(self, msg_hash: str) -> bool:
        if msg_hash in self._processed_hashes:
            return True
        res = self._req("processed_messages", params={"msg_hash": f"eq.{msg_hash}", "select": "msg_hash"})
        if res:
            self._processed_hashes.add(msg_hash)
            return True
        return False

    async def mark_msg_processed(self, msg_hash: str):
        self._processed_hashes.add(msg_hash)
        self._req("processed_messages", method="POST", json_data={"msg_hash": msg_hash}, prefer="resolution=ignore-duplicates")

    # ── Users ───────────────────────────────────────────────────────────────

    def add_user(self, chat_id: int):
        if chat_id not in self._users_cache:
            self._users_cache.add(chat_id)
            self._req("users", method="POST", json_data={"chat_id": chat_id}, prefer="resolution=ignore-duplicates")

    def get_users(self) -> list:
        return list(self._users_cache)

    # ── Number Pool / Stock (Ultra Fast & Realtime Sync) ─────────────────────

    def add_numbers(self, service: str, country: str, numbers) -> int:
        service = service.upper()
        country = country.upper()
        clean_nums = []
        for n in numbers:
            s = str(n).strip()
            if s and s not in clean_nums:
                clean_nums.append(s)

        if not clean_nums:
            return 0

        # Update in-memory stock cache instantly
        with self._cache_lock:
            cur_pool = self._stock_cache.setdefault(service, {}).setdefault(country, [])
            for n in clean_nums:
                if n not in cur_pool:
                    cur_pool.append(n)

        # Batch write to Supabase (500 per batch)
        batch = []
        for n in clean_nums:
            batch.append({"number": n, "service": service, "country": country, "status": "available"})
            if len(batch) >= 500:
                self._req("numbers", method="POST", json_data=batch, prefer="resolution=ignore-duplicates")
                batch = []
        if batch:
            self._req("numbers", method="POST", json_data=batch, prefer="resolution=ignore-duplicates")

        return len(clean_nums)

    def get_stock(self) -> dict:
        """Return stock instantly from memory without remote HTTP latency."""
        with self._cache_lock:
            # Return deep copy so callers can inspect safely
            return {s: {c: list(nums) for c, nums in cty.items()} for s, cty in self._stock_cache.items()}

    def get_number_owner_map(self) -> dict:
        """Instant memory lookup for SMS monitors."""
        with self._cache_lock:
            return dict(self._owner_map)

    def get_number_owner_info(self, number: str):
        with self._cache_lock:
            s = str(number).strip()
            clean = s.lstrip("+")
            return self._owner_info.get(s) or self._owner_info.get(clean) or self._owner_info.get(f"+{clean}") or (None, None)

    def release_user_numbers(self, chat_id: int, service: str, country: str = None) -> int:
        service = service.upper()
        country = country.upper() if country else None
        params = {"owner_chat_id": f"eq.{chat_id}", "service": f"eq.{service}", "status": "eq.assigned"}
        if country:
            params["country"] = f"eq.{country}"
        res = self._req("numbers", method="PATCH", params=params, json_data={"status": "replaced"}, prefer="return=representation")
        return len(res) if isinstance(res, list) else 0

    def reserve_numbers(self, service: str, country: str = None, prefix: str = None, count: int = 5,
                        shuffle: bool = False, chat_id: int = None, owner_name: str = None) -> list:
        service = service.upper()
        country = country.upper() if country else None

        with self._cache_lock:
            country_dict = self._stock_cache.get(service, {})
            if country:
                pool = list(country_dict.get(country, []))
            else:
                pool = [n for nums in country_dict.values() for n in nums]

            if prefix:
                clean_pref = str(prefix).strip().lstrip("+")
                pool = [
                    n for n in pool
                    if str(n).strip().startswith(str(prefix).strip())
                    or str(n).strip().lstrip("+").startswith(clean_pref)
                ]

            if not pool:
                return []

            if shuffle:
                random.shuffle(pool)

            take = pool[:count]
            if not take:
                return []

            take_set = set(take)

            # Instantly remove reserved from in-memory cache
            for c_key in list(country_dict.keys()):
                country_dict[c_key] = [n for n in country_dict[c_key] if n not in take_set]

            # Instantly register ownership in cache
            if chat_id:
                for n in take:
                    n_str = str(n).strip()
                    clean = n_str.lstrip("+")
                    self._owner_map[n_str] = chat_id
                    self._owner_map[clean] = chat_id
                    self._owner_map[f"+{clean}"] = chat_id
                    info = (chat_id, owner_name)
                    self._owner_info[n_str] = info
                    self._owner_info[clean] = info
                    self._owner_info[f"+{clean}"] = info

        # Sync update in Supabase background
        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        num_list_str = f"({','.join(take)})"
        self._req(
            "numbers",
            method="PATCH",
            params={"number": f"in.{num_list_str}"},
            json_data={
                "status": "assigned",
                "owner_chat_id": chat_id,
                "owner_name": owner_name,
                "assigned_at": now_ts
            }
        )
        return take

    def remove_numbers(self, service: str, country: str) -> int:
        service = service.upper()
        country = country.upper()
        with self._cache_lock:
            if service in self._stock_cache and country in self._stock_cache[service]:
                del self._stock_cache[service][country]

        res = self._req("numbers", method="DELETE", params={"service": f"eq.{service}", "country": f"eq.{country}"}, prefer="return=representation")
        return len(res) if isinstance(res, list) else 0

    def get_stock_summary(self) -> list:
        res = self._req("numbers", params={"select": "service,country,status"})
        summary = {}
        if isinstance(res, list):
            for r in res:
                key = (r.get("service"), r.get("country"))
                if key not in summary:
                    summary[key] = {"total": 0, "available": 0, "assigned": 0}
                summary[key]["total"] += 1
                if r.get("status") == "available":
                    summary[key]["available"] += 1
                elif r.get("status") == "assigned":
                    summary[key]["assigned"] += 1

        return [
            (k[0], k[1], v["total"], v["available"], v["assigned"])
            for k, v in sorted(summary.items())
        ]

    def reset_db(self):
        with self._cache_lock:
            self._stock_cache.clear()
            self._owner_map.clear()
            self._owner_info.clear()
        self._req("numbers", method="DELETE", params={"id": "gt.0"})
        self._req("otps", method="DELETE", params={"id": "gt.0"})
        self._req("processed_messages", method="DELETE", params={"created_at": "not.is.null"})

    # ── OTP tracking / reports ──────────────────────────────────────────────

    def log_otp(self, number, service=None, otp=None, msg_hash=None):
        self._req(
            "otps",
            method="POST",
            json_data={"number": str(number), "service": service, "otp": otp, "msg_hash": msg_hash},
            prefer="resolution=ignore-duplicates"
        )

    def otp_summary(self, period="all") -> list:
        otps_res = self._req("otps", params={"select": "number,created_at"})
        if not isinstance(otps_res, list) or not otps_res:
            return []

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        filtered_otps = []
        for o in otps_res:
            if period == "today" and not str(o.get("created_at", "")).startswith(today_str):
                continue
            filtered_otps.append(o.get("number"))

        nums_res = self._req("numbers", params={"select": "number,owner_chat_id,owner_name"})
        num_map = {}
        if isinstance(nums_res, list):
            for nr in nums_res:
                num_map[str(nr.get("number")).lstrip("+")] = (nr.get("owner_name") or "—", nr.get("owner_chat_id"))

        user_summary = {}
        for num in filtered_otps:
            clean = str(num).lstrip("+")
            owner_name, cid = num_map.get(clean, ("—", None))
            key = (owner_name, cid)
            if key not in user_summary:
                user_summary[key] = {"numbers": set(), "count": 0}
            user_summary[key]["numbers"].add(clean)
            user_summary[key]["count"] += 1

        return [
            (k[0], k[1], len(v["numbers"]), v["count"])
            for k, v in sorted(user_summary.items(), key=lambda x: x[1]["count"], reverse=True)
        ]

    def numbers_with_otp(self, period="all") -> list:
        res = self._req("otps", params={"select": "number,created_at"})
        if not isinstance(res, list):
            return []
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        nums = set()
        for r in res:
            if period == "today" and not str(r.get("created_at", "")).startswith(today_str):
                continue
            nums.add(r.get("number"))
        return list(nums)

    def numbers_without_otp(self, period="all") -> list:
        with_otps = set(self.numbers_with_otp(period))
        assigned = self._req("numbers", params={"status": "eq.assigned", "select": "number"})
        res = []
        if isinstance(assigned, list):
            for a in assigned:
                n = a.get("number")
                if n not in with_otps and str(n).lstrip("+") not in with_otps:
                    res.append(n)
        return res

    def get_user_otp_stats_and_history(self, chat_id: int, limit: int = 20):
        """Calculate Bangladesh Time 6AM-6AM stats and return (today_count, total_count, history)."""
        from datetime import datetime, timezone, timedelta
        
        nums_res = self._req("numbers", params={"owner_chat_id": f"eq.{chat_id}", "select": "number"})
        if not isinstance(nums_res, list) or not nums_res:
            return 0, 0, []

        clean_nums = set(str(r["number"]).lstrip("+") for r in nums_res)
        
        # All OTPs for matching
        otps_res = self._req("otps", params={"select": "number,service,otp,created_at", "order": "created_at.desc"})
        if not isinstance(otps_res, list) or not otps_res:
            return 0, 0, []

        # BD Time (UTC+6)
        bd_tz = timezone(timedelta(hours=6))
        now_bd = datetime.now(bd_tz)
        
        # BD 6AM window: if now is before 6AM, window started yesterday at 6AM, else today at 6AM
        if now_bd.hour < 6:
            start_bd = now_bd.replace(hour=6, minute=0, second=0, microsecond=0) - timedelta(days=1)
        else:
            start_bd = now_bd.replace(hour=6, minute=0, second=0, microsecond=0)
        
        # Start in UTC
        start_utc = start_bd.astimezone(timezone.utc)

        today_count = 0
        total_count = 0
        history = []

        for o in otps_res:
            num_clean = str(o.get("number", "")).lstrip("+")
            if num_clean in clean_nums:
                total_count += 1
                created_str = str(o.get("created_at", ""))
                # Parse created_at
                try:
                    dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= start_utc:
                        today_count += 1
                except Exception:
                    pass

                if len(history) < limit:
                    history.append((o.get("number"), o.get("service"), o.get("otp"), o.get("created_at")))

        return today_count, total_count, history

    def get_user_otp_history(self, chat_id: int, limit: int = 20) -> list:
        nums_res = self._req("numbers", params={"owner_chat_id": f"eq.{chat_id}", "select": "number"})
        if not isinstance(nums_res, list) or not nums_res:
            return []
        clean_nums = [str(r["number"]).lstrip("+") for r in nums_res]
        otps_res = self._req("otps", params={"select": "number,service,otp,created_at", "order": "created_at.desc", "limit": str(limit)})
        matches = []
        if isinstance(otps_res, list):
            for o in otps_res:
                if str(o.get("number")).lstrip("+") in clean_nums:
                    matches.append((o.get("number"), o.get("service"), o.get("otp"), o.get("created_at")))
        return matches

    def get_user_numbers(self, chat_id: int) -> list:
        res = self._req("numbers", params={"owner_chat_id": f"eq.{chat_id}", "select": "number,service,country,assigned_at", "order": "assigned_at.desc"})
        if isinstance(res, list):
            return [(r.get("number"), r.get("service"), r.get("country"), r.get("assigned_at")) for r in res]
        return []

    def get_setting(self, key: str, default: str = None) -> str:
        if key in self._settings_cache:
            return self._settings_cache[key]
        res = self._req("settings", params={"key": f"eq.{key}", "select": "value"})
        if isinstance(res, list) and res:
            val = res[0].get("value")
            self._settings_cache[key] = val
            return val
        return default

    def set_setting(self, key: str, value: str):
        self._settings_cache[key] = str(value)
        self._req("settings", method="POST", json_data={"key": key, "value": str(value)}, prefer="resolution=merge-duplicates")

    # ── Dynamic Services ────────────────────────────────────────────────────

    _DEFAULT_SERVICES = [
        {"label": "💬 IMO", "name": "IMO"},
        {"label": "🫆 TELEGRAM", "name": "TELEGRAM"},
        {"label": "🧊 VK", "name": "VK"},
        {"label": "🟢 WHATSAPP", "name": "WHATSAPP"},
    ]

    def get_services(self) -> list:
        if self._services_cache is not None:
            return self._services_cache
        raw = self.get_setting("services")
        if raw:
            try:
                self._services_cache = json.loads(raw)
                return self._services_cache
            except Exception:
                pass
        self._services_cache = list(self._DEFAULT_SERVICES)
        self.save_services(self._DEFAULT_SERVICES)
        return self._services_cache

    def save_services(self, services: list):
        self._services_cache = list(services)
        self.set_setting("services", json.dumps(services, ensure_ascii=False))

    # ── Dynamic Panels Management ───────────────────────────────────────────

    def get_all_panels(self) -> list:
        res = self._req("panels", params={"select": "id,name,base_url,api_key,is_active,created_at", "order": "id.asc"})
        if isinstance(res, list):
            self._panels_cache = [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "base_url": r.get("base_url"),
                    "api_key": r.get("api_key"),
                    "is_active": bool(r.get("is_active")),
                    "created_at": r.get("created_at")
                }
                for r in res
            ]
            return self._panels_cache
        return self._panels_cache or []

    def add_panel(self, name: str, base_url: str, api_key: str) -> bool:
        res = self._req("panels", method="POST", json_data={"name": name.strip(), "base_url": base_url.strip(), "api_key": api_key.strip(), "is_active": 1})
        self.get_all_panels()
        return res is not None

    def toggle_panel_status(self, panel_id: int) -> bool | None:
        res = self._req("panels", params={"id": f"eq.{panel_id}", "select": "is_active"})
        if isinstance(res, list) and res:
            current = bool(res[0].get("is_active"))
            new_status = 0 if current else 1
            self._req("panels", method="PATCH", params={"id": f"eq.{panel_id}"}, json_data={"is_active": new_status})
            self.get_all_panels()
            return bool(new_status)
        return None

    def delete_panel(self, panel_id: int) -> bool:
        res = self._req("panels", method="DELETE", params={"id": f"eq.{panel_id}"}, prefer="return=representation")
        self.get_all_panels()
        return bool(res)


db = Database()
