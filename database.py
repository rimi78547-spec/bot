import sqlite3
import os
import random
import threading
from datetime import datetime, timezone, timedelta

DB_PATH = os.getenv("DB_PATH", "seven.db")


def _period_sql(period):
    if period == "today":
        return "date(created_at) = date('now', 'localtime')", []
    return "1=1", []


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()      # per-thread connection
        self._write_lock = threading.Lock()  # serialize writes
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Return a per-thread SQLite connection with WAL mode enabled."""
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10,
            )
            conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads
            conn.execute("PRAGMA synchronous=NORMAL") # faster writes, still safe
            conn.execute("PRAGMA cache_size=-8000")   # 8 MB cache per connection
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                msg_hash TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE,
                service TEXT NOT NULL,
                country TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                owner_chat_id INTEGER,
                owner_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                assigned_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS otps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL,
                service TEXT,
                otp TEXT,
                msg_hash TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

    # ── processed messages (monitor) ──────────────────────────────────────────

    async def is_msg_processed(self, msg_hash: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE msg_hash = ?", (msg_hash,)
        ).fetchone()
        return row is not None

    async def mark_msg_processed(self, msg_hash: str):
        conn = self._connect()
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (msg_hash) VALUES (?)",
            (msg_hash,),
        )
        conn.commit()

    # ── users ─────────────────────────────────────────────────────────────────

    def add_user(self, chat_id: int):
        conn = self._connect()
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,)
        )
        conn.commit()

    def get_users(self) -> list:
        conn = self._connect()
        rows = conn.execute("SELECT chat_id FROM users").fetchall()
        return [r[0] for r in rows]

    # ── number pool / stock ───────────────────────────────────────────────────

    def add_numbers(self, service: str, country: str, numbers) -> int:
        conn = self._connect()
        added = 0
        for n in numbers:
            n = str(n).strip()
            if not n:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO numbers (number, service, country, status)
                    VALUES (?, ?, ?, 'available')
                    """,
                    (n, service.upper(), country.upper()),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass  # duplicate, skip
        conn.commit()
        return added

    def get_available_counts(self) -> dict:
        """Returns {SERVICE: {COUNTRY: count}} for all available numbers."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT service, country, COUNT(*)
            FROM numbers
            WHERE status = 'available'
            GROUP BY service, country
            """
        ).fetchall()
        result = {}
        for s, c, cnt in rows:
            result.setdefault(s, {})[c] = cnt
        return result

    def get_available_countries_for_service(self, service: str) -> list:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT country, COUNT(*) FROM numbers
            WHERE service = ? AND status = 'available'
            GROUP BY country
            """,
            (service.upper(),),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def assign_numbers(
        self,
        service: str,
        country: str,
        chat_id: int,
        count: int,
        owner_name: str = None,
    ) -> list:
        """Atomically pick `count` random available numbers and assign them."""
        with self._write_lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT id, number FROM numbers
                WHERE service = ? AND country = ? AND status = 'available'
                """,
                (service.upper(), country.upper()),
            ).fetchall()

            if not rows:
                return []

            picked = random.sample(rows, min(count, len(rows)))
            picked_ids = [r[0] for r in picked]
            assigned_at = datetime.now(timezone.utc).isoformat()

            placeholders = ",".join("?" * len(picked_ids))
            conn.execute(
                f"""
                UPDATE numbers
                SET status = 'assigned',
                    owner_chat_id = ?,
                    owner_name = ?,
                    assigned_at = ?
                WHERE id IN ({placeholders})
                """,
                [chat_id, owner_name, assigned_at] + picked_ids,
            )
            conn.commit()
            return [r[1] for r in picked]

    def replace_number(
        self, old_number: str, chat_id: int, owner_name: str = None
    ) -> str | None:
        """Mark old_number as 'replaced', pick a new available one for the same service+country."""
        with self._write_lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT id, service, country FROM numbers
                WHERE number = ? AND owner_chat_id = ? AND status = 'assigned'
                """,
                (old_number, chat_id),
            ).fetchone()

            if not row:
                return None

            _old_id, service, country = row

            new_row = conn.execute(
                """
                SELECT id, number FROM numbers
                WHERE service = ? AND country = ? AND status = 'available'
                ORDER BY RANDOM() LIMIT 1
                """,
                (service, country),
            ).fetchone()

            if not new_row:
                return None  # out of stock

            new_id, new_number = new_row
            assigned_at = datetime.now(timezone.utc).isoformat()

            conn.execute(
                "UPDATE numbers SET status = 'replaced' WHERE number = ?",
                (old_number,),
            )
            conn.execute(
                """
                UPDATE numbers
                SET status = 'assigned',
                    owner_chat_id = ?,
                    owner_name = ?,
                    assigned_at = ?
                WHERE id = ?
                """,
                (chat_id, owner_name, assigned_at, new_id),
            )
            conn.commit()
            return new_number

    def get_number_owner_map(self) -> dict:
        """Return {number: chat_id} for all currently assigned numbers."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT number, owner_chat_id FROM numbers
            WHERE status = 'assigned' AND owner_chat_id IS NOT NULL
            """
        ).fetchall()
        result = {}
        for num, cid in rows:
            clean = str(num).strip().lstrip("+")
            result[str(num).strip()] = cid
            result[clean] = cid
            result[f"+{clean}"] = cid
        return result

    def get_number_owner_info(self, number: str) -> tuple | None:
        """Return (owner_chat_id, owner_name) for a specific number if assigned."""
        clean = str(number).strip().lstrip("+")
        conn = self._connect()
        row = conn.execute(
            """
            SELECT owner_chat_id, owner_name FROM numbers
            WHERE (number = ? OR number = ? OR number = ?)
              AND status = 'assigned' AND owner_chat_id IS NOT NULL
            LIMIT 1
            """,
            (number.strip(), clean, f"+{clean}"),
        ).fetchone()
        return (row[0], row[1]) if row else None

    # ── admin stock management ────────────────────────────────────────────────

    def delete_numbers_by_country(self, country: str) -> int:
        conn = self._connect()
        cur = conn.execute(
            "DELETE FROM numbers WHERE country = ?", (country.upper(),)
        )
        conn.commit()
        return cur.rowcount

    def delete_numbers_by_service(self, service: str) -> int:
        conn = self._connect()
        cur = conn.execute(
            "DELETE FROM numbers WHERE service = ?", (service.upper(),)
        )
        conn.commit()
        return cur.rowcount

    def clear_all_numbers(self) -> int:
        conn = self._connect()
        cur = conn.execute("DELETE FROM numbers")
        conn.commit()
        return cur.rowcount

    def get_stock_summary(self) -> list:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT service, country,
                   SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS avail,
                   SUM(CASE WHEN status = 'assigned'  THEN 1 ELSE 0 END) AS assigned,
                   COUNT(*) AS total
            FROM numbers
            GROUP BY service, country
            ORDER BY service, country
            """
        ).fetchall()
        return rows

    def search_numbers(self, query: str, limit: int = 20) -> list:
        """Search numbers table by partial phone number or country."""
        conn = self._connect()
        clean = query.strip().lstrip("+")
        like_expr = f"%{clean}%"
        rows = conn.execute(
            """
            SELECT number, service, country, status, owner_chat_id, owner_name, assigned_at
            FROM numbers
            WHERE number LIKE ? OR country LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (like_expr, f"%{query.strip().upper()}%", limit),
        ).fetchall()
        return rows

    def reset_assigned_numbers(self, service: str = None, country: str = None) -> int:
        """Reset assigned/replaced numbers back to available."""
        with self._write_lock:
            conn = self._connect()
            conditions = ["status IN ('assigned', 'replaced')"]
            params = []
            if service:
                conditions.append("service = ?")
                params.append(service.upper())
            if country:
                conditions.append("country = ?")
                params.append(country.upper())

            sql = f"""
                UPDATE numbers
                SET status = 'available',
                    owner_chat_id = NULL,
                    owner_name = NULL,
                    assigned_at = NULL
                WHERE {' AND '.join(conditions)}
            """
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def check_existing_numbers(self, numbers: list) -> set:
        """Return the subset of `numbers` that already exist in the database."""
        if not numbers:
            return set()
        conn = self._connect()
        existing = set()
        chunk_size = 500
        clean_nums = [str(n).strip() for n in numbers if str(n).strip()]
        for i in range(0, len(clean_nums), chunk_size):
            chunk = clean_nums[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT number FROM numbers WHERE number IN ({placeholders})",
                chunk,
            ).fetchall()
            existing.update(r[0] for r in rows)
        return existing

    # ── otps ──────────────────────────────────────────────────────────────────

    def log_otp(self, number: str, service: str, otp: str, msg_hash: str):
        conn = self._connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO otps (number, service, otp, msg_hash)
            VALUES (?, ?, ?, ?)
            """,
            (number, service, otp, msg_hash),
        )
        conn.commit()

    def get_otp_stats(self, period: str = "all") -> dict:
        """Returns total OTPs and breakdown by service. period: 'today' or 'all'."""
        conn = self._connect()
        where_clause, args = _period_sql(period)

        total = conn.execute(
            f"SELECT COUNT(*) FROM otps WHERE {where_clause}", args
        ).fetchone()[0]

        by_service = conn.execute(
            f"""
            SELECT service, COUNT(*) FROM otps
            WHERE {where_clause}
            GROUP BY service
            ORDER BY COUNT(*) DESC
            """,
            args,
        ).fetchall()

        by_country = conn.execute(
            f"""
            SELECT n.country, COUNT(o.id)
            FROM otps o
            LEFT JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
            WHERE {where_clause}
            GROUP BY n.country
            ORDER BY COUNT(o.id) DESC
            """,
            args,
        ).fetchall()

        return {
            "total": total,
            "by_service": by_service,
            "by_country": by_country,
        }

    def get_user_otp_stats(self, chat_id: int, period: str = "all") -> dict:
        """Return OTP counts for numbers owned by a specific user."""
        conn = self._connect()
        where_cond, args = _period_sql(period)

        total = conn.execute(
            f"""
            SELECT COUNT(o.id)
            FROM otps o
            INNER JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
            WHERE n.owner_chat_id = ? AND {where_cond}
            """,
            [chat_id] + args,
        ).fetchone()[0]

        by_service = conn.execute(
            f"""
            SELECT o.service, COUNT(o.id)
            FROM otps o
            INNER JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
            WHERE n.owner_chat_id = ? AND {where_cond}
            GROUP BY o.service
            ORDER BY COUNT(o.id) DESC
            """,
            [chat_id] + args,
        ).fetchall()

        return {"total": total, "by_service": by_service}

    def get_unusable_numbers(self, chat_id: int) -> list:
        """Return assigned numbers for chat_id that have NEVER received an OTP."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT n.number
            FROM numbers n
            WHERE n.owner_chat_id = ?
              AND n.status = 'assigned'
              AND NOT EXISTS (
                  SELECT 1 FROM otps o
                  WHERE ltrim(o.number, '+') = ltrim(n.number, '+')
              )
            ORDER BY n.assigned_at DESC
            """,
            (chat_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_user_otp_stats_and_history(self, chat_id: int, limit: int = 20):
        """Calculate Bangladesh Time 6AM-6AM stats and return (today_count, total_count, history)."""
        conn = self._connect()
        
        # All OTPs for numbers owned by this user
        rows = conn.execute(
            """
            SELECT o.number, o.service, o.otp, o.created_at
            FROM otps o
            INNER JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
            WHERE n.owner_chat_id = ?
            ORDER BY o.created_at DESC
            """,
            (chat_id,),
        ).fetchall()

        if not rows:
            return 0, 0, []

        # BD Time (UTC+6)
        bd_tz = timezone(timedelta(hours=6))
        now_bd = datetime.now(bd_tz)
        
        if now_bd.hour < 6:
            start_bd = now_bd.replace(hour=6, minute=0, second=0, microsecond=0) - timedelta(days=1)
        else:
            start_bd = now_bd.replace(hour=6, minute=0, second=0, microsecond=0)
        
        start_utc = start_bd.astimezone(timezone.utc)

        today_count = 0
        total_count = len(rows)
        history = []

        for r in rows:
            created_str = str(r[3])
            try:
                dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= start_utc:
                    today_count += 1
            except Exception:
                pass

            if len(history) < limit:
                history.append((r[0], r[1], r[2], r[3]))

        return today_count, total_count, history

    def get_user_otp_history(self, chat_id: int, limit: int = 20) -> list:
        """Return the last `limit` OTPs received for numbers owned by chat_id.
        Returns list of (number, service, otp, created_at).
        """
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT o.number, o.service, o.otp, o.created_at
            FROM otps o
            INNER JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
            WHERE n.owner_chat_id = ?
            ORDER BY o.created_at DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [tuple(r) for r in rows]

    def get_user_numbers(self, chat_id: int) -> list:
        """Return all numbers currently assigned to chat_id.
        Returns list of (number, service, country, assigned_at).
        """
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT number, service, country, assigned_at
            FROM numbers WHERE owner_chat_id = ?
            ORDER BY assigned_at DESC
            """,
            (chat_id,),
        ).fetchall()
        return [tuple(r) for r in rows]

    def get_setting(self, key: str, default: str = None) -> str:
        conn = self._connect()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        conn = self._connect()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value))
        )
        conn.commit()

    # ── Dynamic services ──────────────────────────────────────────────────────

    _DEFAULT_SERVICES = [
        {"label": "💬 IMO",      "name": "IMO"},
        {"label": "🫆 TELEGRAM", "name": "TELEGRAM"},
        {"label": "🧊 VK",       "name": "VK"},
        {"label": "🟢 WHATSAPP", "name": "WHATSAPP"},
    ]

    def get_services(self) -> list:
        """Return list of {label, name} dicts. Seeds defaults if not yet saved."""
        import json
        raw = self.get_setting("services")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        self.save_services(self._DEFAULT_SERVICES)
        return list(self._DEFAULT_SERVICES)

    def save_services(self, services: list):
        """Persist services list (list of {label, name}) to settings."""
        import json
        self.set_setting("services", json.dumps(services, ensure_ascii=False))

    # ── Dynamic Panels Management ─────────────────────────────────────────────

    def get_all_panels(self) -> list:
        """Return all panels as dicts: [{id, name, base_url, api_key, is_active, created_at}]"""
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, name, base_url, api_key, is_active, created_at FROM panels ORDER BY id ASC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "base_url": r[2],
                "api_key": r[3],
                "is_active": bool(r[4]),
                "created_at": r[5]
            }
            for r in rows
        ]

    def add_panel(self, name: str, base_url: str, api_key: str) -> bool:
        """Add a new panel into the database."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO panels (name, base_url, api_key, is_active) VALUES (?, ?, ?, 1)",
                (name.strip(), base_url.strip(), api_key.strip())
            )
            conn.commit()
            return True
        except Exception:
            return False

    def toggle_panel_status(self, panel_id: int) -> bool | None:
        """Toggle panel active/stopped status. Returns new is_active status or None if not found."""
        conn = self._connect()
        row = conn.execute("SELECT is_active FROM panels WHERE id = ?", (panel_id,)).fetchone()
        if not row:
            return None
        new_status = 0 if row[0] else 1
        conn.execute("UPDATE panels SET is_active = ? WHERE id = ?", (new_status, panel_id))
        conn.commit()
        return bool(new_status)

    def delete_panel(self, panel_id: int) -> bool:
        """Delete a panel by ID."""
        conn = self._connect()
        cur = conn.execute("DELETE FROM panels WHERE id = ?", (panel_id,))
        conn.commit()
        return cur.rowcount > 0


db = Database()
