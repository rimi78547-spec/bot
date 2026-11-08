import os
import random
import threading
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime

logger = logging.getLogger(__name__)

# Supabase REST Configuration (Works anywhere on standard HTTPS, no IPv6 issues)
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "https://ovhqorkmwipxpzbxzywh.supabase.co").strip().rstrip("/")
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "sb_publishable_Odvhc7PLvccbY9pleKOQmg_xM6Wo-mo").strip()


class Database:
    def __init__(self, base_url: str = SUPABASE_URL, api_key: str = SUPABASE_KEY):
        self.base_url = base_url
        self.api_key = api_key
        self.rest_url = f"{self.base_url}/rest/v1"
        self._local = threading.local()
        logger.info(f"Initialized Cloud Supabase REST client at {self.rest_url}")

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
            with urllib.request.urlopen(req, timeout=15) as res:
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

    # ── Processed messages ──────────────────────────────────────────────────

    async def is_msg_processed(self, msg_hash: str) -> bool:
        res = self._req("processed_messages", params={"msg_hash": f"eq.{msg_hash}", "select": "msg_hash"})
        return bool(res)

    async def mark_msg_processed(self, msg_hash: str):
        self._req(
            "processed_messages",
            method="POST",
            json_data={"msg_hash": msg_hash},
            prefer="resolution=ignore-duplicates"
        )

    # ── Users ───────────────────────────────────────────────────────────────

    def add_user(self, chat_id: int):
        self._req("users", method="POST", json_data={"chat_id": chat_id}, prefer="resolution=ignore-duplicates")

    def get_users(self) -> list:
        res = self._req("users", params={"select": "chat_id"})
        if isinstance(res, list):
            return [r["chat_id"] for r in res]
        return []

    # ── Number pool / stock ─────────────────────────────────────────────────

    def add_numbers(self, service: str, country: str, numbers) -> int:
        batch = []
        for n in numbers:
            n = str(n).strip()
            if not n:
                continue
            batch.append({"number": n, "service": service, "country": country, "status": "available"})
            if len(batch) >= 500:
                self._req("numbers", method="POST", json_data=batch, prefer="resolution=ignore-duplicates")
                batch = []
        if batch:
            self._req("numbers", method="POST", json_data=batch, prefer="resolution=ignore-duplicates")
        return len(numbers)

    def get_stock(self) -> dict:
        # Fetch available numbers
        res = self._req("numbers", params={"status": "eq.available", "select": "service,country,number"})
        stock = {}
        if isinstance(res, list):
            for r in res:
                s, c, n = r.get("service"), r.get("country"), r.get("number")
                stock.setdefault(s, {}).setdefault(c, []).append(n)
        return stock

    def get_number_owner_map(self) -> dict:
        res = self._req("numbers", params={"owner_chat_id": "not.is.null", "status": "in.(assigned,replaced)", "select": "number,owner_chat_id"})
        mapping = {}
        if isinstance(res, list):
            for r in res:
                n, c = r.get("number"), r.get("owner_chat_id")
                s = str(n).strip()
                clean = s.lstrip("+")
                mapping[s] = c
                mapping[clean] = c
                mapping[f"+{clean}"] = c
        return mapping

    def get_number_owner_info(self, number: str):
        s = str(number).strip()
        clean = s.lstrip("+")
        # Match any of number, clean, +clean
        cond = f"number.in.({s},{clean},+{clean}),owner_chat_id.not.is.null"
        res = self._req("numbers", params={"or": f"(number.eq.{s},number.eq.{clean},number.eq.+{clean})", "owner_chat_id": "not.is.null", "select": "owner_chat_id,owner_name", "limit": "1"})
        if isinstance(res, list) and res:
            return res[0].get("owner_chat_id"), res[0].get("owner_name")
        return None, None

    def release_user_numbers(self, chat_id: int, service: str, country: str = None) -> int:
        params = {"owner_chat_id": f"eq.{chat_id}", "service": f"eq.{service}", "status": "eq.assigned"}
        if country:
            params["country"] = f"eq.{country}"
        res = self._req("numbers", method="PATCH", params=params, json_data={"status": "replaced"}, prefer="return=representation")
        return len(res) if isinstance(res, list) else 0

    def reserve_numbers(self, service, country=None, prefix=None, count=5,
                        shuffle=False, chat_id=None, owner_name=None) -> list:
        params = {"service": f"eq.{service}", "status": "eq.available", "select": "number"}
        if country:
            params["country"] = f"eq.{country}"
        if prefix:
            clean_pref = str(prefix).strip().lstrip("+")
            params["or"] = f"(number.like.{prefix}*,number.like.+{clean_pref}*,number.like.{clean_pref}*)"

        res = self._req("numbers", params=params)
        pool = [r["number"] for r in res] if isinstance(res, list) else []
        if not pool:
            return []

        if shuffle:
            random.shuffle(pool)
        take = pool[:count]
        if not take:
            return []

        num_list_str = f"({','.join(take)})"
        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
        res = self._req("numbers", method="DELETE", params={"service": f"eq.{service}", "country": f"eq.{country}"}, prefer="return=representation")
        return len(res) if isinstance(res, list) else 0

    def get_stock_summary(self) -> list:
        # Group summary from database
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
        # Fetch OTPs and cross with numbers
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
        res = self._req("settings", params={"key": f"eq.{key}", "select": "value"})
        if isinstance(res, list) and res:
            return res[0].get("value")
        return default

    def set_setting(self, key: str, value: str):
        self._req("settings", method="POST", json_data={"key": key, "value": str(value)}, prefer="resolution=merge-duplicates")

    # ── Dynamic Services ────────────────────────────────────────────────────

    _DEFAULT_SERVICES = [
        {"label": "💬 IMO", "name": "IMO"},
        {"label": "🫆 TELEGRAM", "name": "TELEGRAM"},
        {"label": "🧊 VK", "name": "VK"},
        {"label": "🟢 WHATSAPP", "name": "WHATSAPP"},
    ]

    def get_services(self) -> list:
        raw = self.get_setting("services")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        self.save_services(self._DEFAULT_SERVICES)
        return list(self._DEFAULT_SERVICES)

    def save_services(self, services: list):
        self.set_setting("services", json.dumps(services, ensure_ascii=False))

    # ── Dynamic Panels Management ───────────────────────────────────────────

    def get_all_panels(self) -> list:
        res = self._req("panels", params={"select": "id,name,base_url,api_key,is_active,created_at", "order": "id.asc"})
        if isinstance(res, list):
            return [
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
        return []

    def add_panel(self, name: str, base_url: str, api_key: str) -> bool:
        res = self._req("panels", method="POST", json_data={"name": name.strip(), "base_url": base_url.strip(), "api_key": api_key.strip(), "is_active": 1})
        return res is not None

    def toggle_panel_status(self, panel_id: int) -> bool | None:
        res = self._req("panels", params={"id": f"eq.{panel_id}", "select": "is_active"})
        if isinstance(res, list) and res:
            current = bool(res[0].get("is_active"))
            new_status = 0 if current else 1
            self._req("panels", method="PATCH", params={"id": f"eq.{panel_id}"}, json_data={"is_active": new_status})
            return bool(new_status)
        return None

    def delete_panel(self, panel_id: int) -> bool:
        res = self._req("panels", method="DELETE", params={"id": f"eq.{panel_id}"}, prefer="return=representation")
        return bool(res)


db = Database()
