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

    def get_stock(self) -> dict:
        """Returns {SERVICE: {COUNTRY: [numbers]}} for all available numbers."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT service, country, number FROM numbers WHERE status = 'available'"
        ).fetchall()
        stock = {}
        for service, country, number in rows:
            stock.setdefault(service, {}).setdefault(country, []).append(number)
        return stock

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

    def get_number_owner_map(self) -> dict:
        """Return {number: chat_id} for all assigned and replaced numbers."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT number, owner_chat_id FROM numbers
            WHERE owner_chat_id IS NOT NULL AND status IN ('assigned', 'replaced')
            """
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
            """
            SELECT owner_chat_id, owner_name FROM numbers
            WHERE (number = ? OR number = ? OR number = ?) AND owner_chat_id IS NOT NULL
            LIMIT 1
            """,
            (s, clean, f"+{clean}")
        ).fetchone()
        if row:
            return row[0], row[1]
        return None, None

    def release_user_numbers(self, chat_id: int, service: str, country: str = None) -> int:
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

    # ── otps ──────────────────────────────────────────────────────────────────

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
        """Return the last `limit` OTPs received for numbers owned by chat_id."""
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
        """Return all numbers currently assigned to chat_id."""
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
