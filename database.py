import sqlite3
import os
import random
import threading

DB_PATH = os.getenv("DB_PATH", "seven.db")


def _period_sql(period):
    if period == "today":
        return "date(created_at) = date('now', 'localtime')", []
    return "1=1", []


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()   # per-thread connection
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
            cur = conn.execute(
                "INSERT OR IGNORE INTO numbers (number, service, country) VALUES (?, ?, ?)",
                (n, service, country),
            )
            added += cur.rowcount
        conn.commit()
        return added

    def get_stock(self) -> dict:
        conn = self._connect()
        rows = conn.execute(
            "SELECT service, country, number FROM numbers WHERE status = 'available'"
        ).fetchall()
        stock = {}
        for service, country, number in rows:
            stock.setdefault(service, {}).setdefault(country, []).append(number)
        return stock

    def get_number_owner_map(self) -> dict:
        conn = self._connect()
        # Include both 'assigned' and 'replaced' so late OTPs still reach the original owner
        rows = conn.execute(
            "SELECT number, owner_chat_id FROM numbers WHERE owner_chat_id IS NOT NULL AND status IN ('assigned', 'replaced')"
        ).fetchall()
        mapping = {}
        for n, c in rows:
            s = str(n).strip()
            clean = s.lstrip('+')
            mapping[s] = c
            mapping[clean] = c
            mapping[f"+{clean}"] = c
        return mapping

    def get_number_owner_info(self, number: str):
        conn = self._connect()
        s = str(number).strip()
        clean = s.lstrip('+')
        row = conn.execute(
            "SELECT owner_chat_id, owner_name FROM numbers WHERE (number = ? OR number = ? OR number = ?) AND owner_chat_id IS NOT NULL LIMIT 1",
            (s, clean, f"+{clean}")
        ).fetchone()
        if row:
            return row[0], row[1]
        return None, None

    def release_user_numbers(self, chat_id: int, service: str, country: str = None) -> int:
        """Mark a user's currently-assigned numbers as 'replaced'.
        
        The numbers are removed from the active assignment pool (so the user no
        longer sees them) but owner_chat_id is kept so that any late-arriving
        OTPs for those numbers are still routed to the correct user.
        """
        conn = self._connect()
        where = ["owner_chat_id = ?", "service = ?", "status = 'assigned'"]
        args = [chat_id, service]
        if country:
            where.append("country = ?")
            args.append(country)
        cur = conn.execute(
            f"UPDATE numbers SET status = 'replaced' WHERE {' AND '.join(where)}",
            args,
        )
        conn.commit()
        return cur.rowcount


    def reserve_numbers(self, service, country=None, prefix=None, count=5,
                        shuffle=False, chat_id=None, owner_name=None) -> list:
        conn = self._connect()
        where = ["service = ?", "status = 'available'"]
        args = [service]
        if country:
            where.append("country = ?")
            args.append(country)
        rows = conn.execute(
            f"SELECT number FROM numbers WHERE {' AND '.join(where)}", args
        ).fetchall()
        pool = [r[0] for r in rows]
        if prefix:
            clean_pref = str(prefix).strip().lstrip('+')
            pool = [
                n for n in pool
                if str(n).strip().startswith(str(prefix).strip())
                or str(n).strip().lstrip('+').startswith(clean_pref)
            ]
        if shuffle:
            random.shuffle(pool)
        take = pool[:count]
        if not take:
            return []
        qmarks = ",".join("?" for _ in take)
        conn.execute(
            f"UPDATE numbers SET status = 'assigned', owner_chat_id = ?, owner_name = ?, "
            f"assigned_at = CURRENT_TIMESTAMP WHERE number IN ({qmarks})",
            [chat_id, owner_name] + take,
        )
        conn.commit()
        return take

    def remove_numbers(self, service: str, country: str) -> int:
        """Remove ALL numbers (available + assigned) for a given service + country."""
        conn = self._connect()
        cur = conn.execute(
            "DELETE FROM numbers WHERE service = ? AND country = ?",
            (service, country),
        )
        conn.commit()
        return cur.rowcount

    def get_stock_summary(self) -> list:
        """Return list of (service, country, total_count, available_count, assigned_count)."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT service, country, COUNT(*) as total, "
            "SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) as avail, "
            "SUM(CASE WHEN status = 'assigned' THEN 1 ELSE 0 END) as assigned "
            "FROM numbers GROUP BY service, country ORDER BY service, country"
        ).fetchall()
        return [tuple(r) for r in rows]

    def reset_db(self):
        """Drop all data tables and recreate them (full reset)."""
        conn = self._connect()
        conn.execute("DELETE FROM numbers")
        conn.execute("DELETE FROM otps")
        conn.execute("DELETE FROM processed_messages")
        conn.commit()

    # ── OTP tracking / reports ────────────────────────────────────────────────

    def log_otp(self, number, service=None, otp=None, msg_hash=None):
        conn = self._connect()
        conn.execute(
            "INSERT OR IGNORE INTO otps (number, service, otp, msg_hash) VALUES (?, ?, ?, ?)",
            (str(number), service, otp, msg_hash),
        )
        conn.commit()

    def otp_summary(self, period="all") -> list:
        cond, args = _period_sql(period)
        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT COALESCE(n.owner_name, '—'), n.owner_chat_id,
                   COUNT(DISTINCT o.number) AS num_numbers,
                   COUNT(*) AS total_otps
            FROM otps o
            LEFT JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
            WHERE {cond}
            GROUP BY n.owner_chat_id, n.owner_name
            ORDER BY total_otps DESC
            """,
            args,
        ).fetchall()
        return [tuple(r) for r in rows]

    def numbers_with_otp(self, period="all") -> list:
        cond, args = _period_sql(period)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT DISTINCT number FROM otps WHERE {cond}", args
        ).fetchall()
        return [r[0] for r in rows]

    def numbers_without_otp(self, period="all") -> list:
        cond, args = _period_sql(period)
        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT DISTINCT n.number FROM numbers n
            WHERE n.status = 'assigned'
              AND NOT EXISTS (
                  SELECT 1 FROM otps o
                  WHERE ltrim(o.number, '+') = ltrim(n.number, '+') AND {cond}
              )
            """,
            args,
        ).fetchall()
        return [r[0] for r in rows]

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
        # Seed defaults on first call
        self.save_services(self._DEFAULT_SERVICES)
        return list(self._DEFAULT_SERVICES)

    def save_services(self, services: list):
        """Persist services list (list of {label, name}) to settings."""
        import json
        self.set_setting("services", json.dumps(services, ensure_ascii=False))


db = Database()