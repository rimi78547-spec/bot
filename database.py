import os
import random
import threading
import json
import logging
import urllib.parse
from datetime import datetime

logger = logging.getLogger(__name__)

# Cloud Supabase / PostgreSQL URI or local SQLite fallback
SUPABASE_DB_URL = os.getenv("DATABASE_URL")
if not SUPABASE_DB_URL:
    # Build from Supabase credentials if given
    _db_pass = os.getenv("SUPABASE_DB_PASSWORD") or "@Vu!%K3_KzVkr,N"
    _quoted_pass = urllib.parse.quote(_db_pass)
    _db_host = os.getenv("SUPABASE_DB_HOST") or "db.ovhqorkmwipxpzbxzywh.supabase.co"
    SUPABASE_DB_URL = f"postgresql://postgres:{_quoted_pass}@{_db_host}:5432/postgres"

# Check if psycopg2 is available
_HAS_PSYCOPG2 = False
try:
    import psycopg2
    from psycopg2 import pool
    _HAS_PSYCOPG2 = True
except ImportError:
    import sqlite3


class Database:
    def __init__(self, db_url: str = SUPABASE_DB_URL, sqlite_path: str = "seven.db"):
        self.db_url = db_url
        self.sqlite_path = sqlite_path
        self._local = threading.local()
        self._pool = None
        self.is_postgres = False

        if _HAS_PSYCOPG2 and self.db_url and self.db_url.startswith("postgres"):
            try:
                self._pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=20,
                    dsn=self.db_url,
                    connect_timeout=10
                )
                self.is_postgres = True
                logger.info("Connected to Cloud PostgreSQL / Supabase Database!")
            except Exception as e:
                logger.error(f"Failed to connect to Supabase PostgreSQL: {e}. Falling back to SQLite.")
                self.is_postgres = False

        self._init_db()

    def _get_conn(self):
        if self.is_postgres and self._pool:
            return self._pool.getconn()
        
        # SQLite fallback
        import sqlite3
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _release_conn(self, conn):
        if self.is_postgres and self._pool:
            try:
                self._pool.putconn(conn)
            except Exception:
                pass

    def _init_db(self):
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS processed_messages (
                            msg_hash TEXT PRIMARY KEY,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE TABLE IF NOT EXISTS numbers (
                            id SERIAL PRIMARY KEY,
                            number TEXT NOT NULL UNIQUE,
                            service TEXT NOT NULL,
                            country TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'available',
                            owner_chat_id BIGINT,
                            owner_name TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            assigned_at TIMESTAMP
                        );
                        CREATE TABLE IF NOT EXISTS otps (
                            id SERIAL PRIMARY KEY,
                            number TEXT NOT NULL,
                            service TEXT,
                            otp TEXT,
                            msg_hash TEXT UNIQUE,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE TABLE IF NOT EXISTS users (
                            chat_id BIGINT PRIMARY KEY,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE TABLE IF NOT EXISTS settings (
                            key TEXT PRIMARY KEY,
                            value TEXT
                        );
                        CREATE TABLE IF NOT EXISTS panels (
                            id SERIAL PRIMARY KEY,
                            name TEXT NOT NULL UNIQUE,
                            base_url TEXT NOT NULL,
                            api_key TEXT NOT NULL,
                            is_active INTEGER DEFAULT 1,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    conn.commit()
            else:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_messages (
                        msg_hash TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.execute("""
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
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS otps (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        number TEXT NOT NULL,
                        service TEXT,
                        otp TEXT,
                        msg_hash TEXT UNIQUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id INTEGER PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS panels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        base_url TEXT NOT NULL,
                        api_key TEXT NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()
        finally:
            self._release_conn(conn)

    # ── Processed messages ──────────────────────────────────────────────────

    async def is_msg_processed(self, msg_hash: str) -> bool:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM processed_messages WHERE msg_hash = %s", (msg_hash,))
                    return cur.fetchone() is not None
            else:
                row = conn.execute("SELECT 1 FROM processed_messages WHERE msg_hash = ?", (msg_hash,)).fetchone()
                return row is not None
        finally:
            self._release_conn(conn)

    async def mark_msg_processed(self, msg_hash: str):
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO processed_messages (msg_hash) VALUES (%s) ON CONFLICT DO NOTHING",
                        (msg_hash,)
                    )
                    conn.commit()
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO processed_messages (msg_hash) VALUES (?)",
                    (msg_hash,)
                )
                conn.commit()
        finally:
            self._release_conn(conn)

    # ── Users ───────────────────────────────────────────────────────────────

    def add_user(self, chat_id: int):
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
                    conn.commit()
            else:
                conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
                conn.commit()
        finally:
            self._release_conn(conn)

    def get_users(self) -> list:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("SELECT chat_id FROM users")
                    return [r[0] for r in cur.fetchall()]
            else:
                rows = conn.execute("SELECT chat_id FROM users").fetchall()
                return [r[0] for r in rows]
        finally:
            self._release_conn(conn)

    # ── Number pool / stock ─────────────────────────────────────────────────

    def add_numbers(self, service: str, country: str, numbers) -> int:
        conn = self._get_conn()
        added = 0
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    for n in numbers:
                        n = str(n).strip()
                        if not n:
                            continue
                        cur.execute(
                            "INSERT INTO numbers (number, service, country) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (n, service, country)
                        )
                        added += cur.rowcount
                    conn.commit()
            else:
                for n in numbers:
                    n = str(n).strip()
                    if not n:
                        continue
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO numbers (number, service, country) VALUES (?, ?, ?)",
                        (n, service, country)
                    )
                    added += cur.rowcount
                conn.commit()
            return added
        finally:
            self._release_conn(conn)

    def get_stock(self) -> dict:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("SELECT service, country, number FROM numbers WHERE status = 'available'")
                    rows = cur.fetchall()
            else:
                rows = conn.execute("SELECT service, country, number FROM numbers WHERE status = 'available'").fetchall()
            stock = {}
            for service, country, number in rows:
                stock.setdefault(service, {}).setdefault(country, []).append(number)
            return stock
        finally:
            self._release_conn(conn)

    def get_number_owner_map(self) -> dict:
        conn = self._get_conn()
        try:
            query = "SELECT number, owner_chat_id FROM numbers WHERE owner_chat_id IS NOT NULL AND status IN ('assigned', 'replaced')"
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall()
            else:
                rows = conn.execute(query).fetchall()

            mapping = {}
            for n, c in rows:
                s = str(n).strip()
                clean = s.lstrip("+")
                mapping[s] = c
                mapping[clean] = c
                mapping[f"+{clean}"] = c
            return mapping
        finally:
            self._release_conn(conn)

    def get_number_owner_info(self, number: str):
        conn = self._get_conn()
        try:
            s = str(number).strip()
            clean = s.lstrip("+")
            q_pg = "SELECT owner_chat_id, owner_name FROM numbers WHERE (number = %s OR number = %s OR number = %s) AND owner_chat_id IS NOT NULL LIMIT 1"
            q_sq = "SELECT owner_chat_id, owner_name FROM numbers WHERE (number = ? OR number = ? OR number = ?) AND owner_chat_id IS NOT NULL LIMIT 1"
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(q_pg, (s, clean, f"+{clean}"))
                    row = cur.fetchone()
            else:
                row = conn.execute(q_sq, (s, clean, f"+{clean}")).fetchone()
            if row:
                return row[0], row[1]
            return None, None
        finally:
            self._release_conn(conn)

    def release_user_numbers(self, chat_id: int, service: str, country: str = None) -> int:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                where = ["owner_chat_id = %s", "service = %s", "status = 'assigned'"]
                args = [chat_id, service]
                if country:
                    where.append("country = %s")
                    args.append(country)
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE numbers SET status = 'replaced' WHERE {' AND '.join(where)}", args)
                    conn.commit()
                    return cur.rowcount
            else:
                where = ["owner_chat_id = ?", "service = ?", "status = 'assigned'"]
                args = [chat_id, service]
                if country:
                    where.append("country = ?")
                    args.append(country)
                cur = conn.execute(f"UPDATE numbers SET status = 'replaced' WHERE {' AND '.join(where)}", args)
                conn.commit()
                return cur.rowcount
        finally:
            self._release_conn(conn)

    def reserve_numbers(self, service, country=None, prefix=None, count=5,
                        shuffle=False, chat_id=None, owner_name=None) -> list:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                where = ["service = %s", "status = 'available'"]
                args = [service]
                if country:
                    where.append("country = %s")
                    args.append(country)
                with conn.cursor() as cur:
                    cur.execute(f"SELECT number FROM numbers WHERE {' AND '.join(where)}", args)
                    rows = cur.fetchall()
            else:
                where = ["service = ?", "status = 'available'"]
                args = [service]
                if country:
                    where.append("country = ?")
                    args.append(country)
                rows = conn.execute(f"SELECT number FROM numbers WHERE {' AND '.join(where)}", args).fetchall()

            pool = [r[0] for r in rows]
            if prefix:
                clean_pref = str(prefix).strip().lstrip("+")
                pool = [
                    n for n in pool
                    if str(n).strip().startswith(str(prefix).strip())
                    or str(n).strip().lstrip("+").startswith(clean_pref)
                ]
            if shuffle:
                random.shuffle(pool)
            take = pool[:count]
            if not take:
                return []

            if self.is_postgres:
                with conn.cursor() as cur:
                    placeholders = ",".join("%s" for _ in take)
                    cur.execute(
                        f"UPDATE numbers SET status = 'assigned', owner_chat_id = %s, owner_name = %s, "
                        f"assigned_at = CURRENT_TIMESTAMP WHERE number IN ({placeholders})",
                        [chat_id, owner_name] + take
                    )
                    conn.commit()
            else:
                placeholders = ",".join("?" for _ in take)
                conn.execute(
                    f"UPDATE numbers SET status = 'assigned', owner_chat_id = ?, owner_name = ?, "
                    f"assigned_at = CURRENT_TIMESTAMP WHERE number IN ({placeholders})",
                    [chat_id, owner_name] + take
                )
                conn.commit()
            return take
        finally:
            self._release_conn(conn)

    def remove_numbers(self, service: str, country: str) -> int:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM numbers WHERE service = %s AND country = %s", (service, country))
                    conn.commit()
                    return cur.rowcount
            else:
                cur = conn.execute("DELETE FROM numbers WHERE service = ? AND country = ?", (service, country))
                conn.commit()
                return cur.rowcount
        finally:
            self._release_conn(conn)

    def get_stock_summary(self) -> list:
        conn = self._get_conn()
        try:
            query = (
                "SELECT service, country, COUNT(*) as total, "
                "SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) as avail, "
                "SUM(CASE WHEN status = 'assigned' THEN 1 ELSE 0 END) as assigned "
                "FROM numbers GROUP BY service, country ORDER BY service, country"
            )
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(query)
                    return [tuple(r) for r in cur.fetchall()]
            else:
                rows = conn.execute(query).fetchall()
                return [tuple(r) for r in rows]
        finally:
            self._release_conn(conn)

    def reset_db(self):
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM numbers; DELETE FROM otps; DELETE FROM processed_messages;")
                    conn.commit()
            else:
                conn.execute("DELETE FROM numbers")
                conn.execute("DELETE FROM otps")
                conn.execute("DELETE FROM processed_messages")
                conn.commit()
        finally:
            self._release_conn(conn)

    # ── OTP tracking / reports ──────────────────────────────────────────────

    def log_otp(self, number, service=None, otp=None, msg_hash=None):
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO otps (number, service, otp, msg_hash) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (str(number), service, otp, msg_hash)
                    )
                    conn.commit()
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO otps (number, service, otp, msg_hash) VALUES (?, ?, ?, ?)",
                    (str(number), service, otp, msg_hash)
                )
                conn.commit()
        finally:
            self._release_conn(conn)

    def otp_summary(self, period="all") -> list:
        cond_pg = "date(created_at) = CURRENT_DATE" if period == "today" else "1=1"
        cond_sq = "date(created_at) = date('now', 'localtime')" if period == "today" else "1=1"
        conn = self._get_conn()
        try:
            if self.is_postgres:
                query = f"""
                    SELECT COALESCE(n.owner_name, '—'), n.owner_chat_id,
                           COUNT(DISTINCT o.number) AS num_numbers,
                           COUNT(*) AS total_otps
                    FROM otps o
                    LEFT JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
                    WHERE {cond_pg}
                    GROUP BY n.owner_chat_id, n.owner_name
                    ORDER BY total_otps DESC
                """
                with conn.cursor() as cur:
                    cur.execute(query)
                    return [tuple(r) for r in cur.fetchall()]
            else:
                query = f"""
                    SELECT COALESCE(n.owner_name, '—'), n.owner_chat_id,
                           COUNT(DISTINCT o.number) AS num_numbers,
                           COUNT(*) AS total_otps
                    FROM otps o
                    LEFT JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
                    WHERE {cond_sq}
                    GROUP BY n.owner_chat_id, n.owner_name
                    ORDER BY total_otps DESC
                """
                rows = conn.execute(query).fetchall()
                return [tuple(r) for r in rows]
        finally:
            self._release_conn(conn)

    def numbers_with_otp(self, period="all") -> list:
        cond = "date(created_at) = CURRENT_DATE" if period == "today" else "1=1"
        cond_sq = "date(created_at) = date('now', 'localtime')" if period == "today" else "1=1"
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT DISTINCT number FROM otps WHERE {cond}")
                    return [r[0] for r in cur.fetchall()]
            else:
                rows = conn.execute(f"SELECT DISTINCT number FROM otps WHERE {cond_sq}").fetchall()
                return [r[0] for r in rows]
        finally:
            self._release_conn(conn)

    def numbers_without_otp(self, period="all") -> list:
        cond = "date(o.created_at) = CURRENT_DATE" if period == "today" else "1=1"
        cond_sq = "date(o.created_at) = date('now', 'localtime')" if period == "today" else "1=1"
        conn = self._get_conn()
        try:
            if self.is_postgres:
                query = f"""
                    SELECT DISTINCT n.number FROM numbers n
                    WHERE n.status = 'assigned'
                      AND NOT EXISTS (
                          SELECT 1 FROM otps o
                          WHERE ltrim(o.number, '+') = ltrim(n.number, '+') AND {cond}
                      )
                """
                with conn.cursor() as cur:
                    cur.execute(query)
                    return [r[0] for r in cur.fetchall()]
            else:
                query = f"""
                    SELECT DISTINCT n.number FROM numbers n
                    WHERE n.status = 'assigned'
                      AND NOT EXISTS (
                          SELECT 1 FROM otps o
                          WHERE ltrim(o.number, '+') = ltrim(n.number, '+') AND {cond_sq}
                      )
                """
                rows = conn.execute(query).fetchall()
                return [r[0] for r in rows]
        finally:
            self._release_conn(conn)

    def get_user_otp_history(self, chat_id: int, limit: int = 20) -> list:
        conn = self._get_conn()
        try:
            query_pg = """
                SELECT o.number, o.service, o.otp, o.created_at
                FROM otps o
                INNER JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
                WHERE n.owner_chat_id = %s
                ORDER BY o.created_at DESC
                LIMIT %s
            """
            query_sq = """
                SELECT o.number, o.service, o.otp, o.created_at
                FROM otps o
                INNER JOIN numbers n ON ltrim(o.number, '+') = ltrim(n.number, '+')
                WHERE n.owner_chat_id = ?
                ORDER BY o.created_at DESC
                LIMIT ?
            """
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(query_pg, (chat_id, limit))
                    return [tuple(r) for r in cur.fetchall()]
            else:
                rows = conn.execute(query_sq, (chat_id, limit)).fetchall()
                return [tuple(r) for r in rows]
        finally:
            self._release_conn(conn)

    def get_user_numbers(self, chat_id: int) -> list:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT number, service, country, assigned_at FROM numbers WHERE owner_chat_id = %s ORDER BY assigned_at DESC",
                        (chat_id,)
                    )
                    return [tuple(r) for r in cur.fetchall()]
            else:
                rows = conn.execute(
                    "SELECT number, service, country, assigned_at FROM numbers WHERE owner_chat_id = ? ORDER BY assigned_at DESC",
                    (chat_id,)
                ).fetchall()
                return [tuple(r) for r in rows]
        finally:
            self._release_conn(conn)

    def get_setting(self, key: str, default: str = None) -> str:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
                    row = cur.fetchone()
                    return row[0] if row else default
            else:
                row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                return row[0] if row else default
        finally:
            self._release_conn(conn)

    def set_setting(self, key: str, value: str):
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                        (key, str(value))
                    )
                    conn.commit()
            else:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value))
                )
                conn.commit()
        finally:
            self._release_conn(conn)

    # ── Dynamic services ────────────────────────────────────────────────────

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
        conn = self._get_conn()
        try:
            query = "SELECT id, name, base_url, api_key, is_active, created_at FROM panels ORDER BY id ASC"
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall()
            else:
                rows = conn.execute(query).fetchall()

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
        finally:
            self._release_conn(conn)

    def add_panel(self, name: str, base_url: str, api_key: str) -> bool:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO panels (name, base_url, api_key, is_active) VALUES (%s, %s, %s, 1)",
                        (name.strip(), base_url.strip(), api_key.strip())
                    )
                    conn.commit()
                    return True
            else:
                conn.execute(
                    "INSERT INTO panels (name, base_url, api_key, is_active) VALUES (?, ?, ?, 1)",
                    (name.strip(), base_url.strip(), api_key.strip())
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding panel: {e}")
            return False
        finally:
            self._release_conn(conn)

    def toggle_panel_status(self, panel_id: int) -> bool | None:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("SELECT is_active FROM panels WHERE id = %s", (panel_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    new_status = 0 if row[0] else 1
                    cur.execute("UPDATE panels SET is_active = %s WHERE id = %s", (new_status, panel_id))
                    conn.commit()
                    return bool(new_status)
            else:
                row = conn.execute("SELECT is_active FROM panels WHERE id = ?", (panel_id,)).fetchone()
                if not row:
                    return None
                new_status = 0 if row[0] else 1
                conn.execute("UPDATE panels SET is_active = ? WHERE id = ?", (new_status, panel_id))
                conn.commit()
                return bool(new_status)
        finally:
            self._release_conn(conn)

    def delete_panel(self, panel_id: int) -> bool:
        conn = self._get_conn()
        try:
            if self.is_postgres:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM panels WHERE id = %s", (panel_id,))
                    conn.commit()
                    return cur.rowcount > 0
            else:
                cur = conn.execute("DELETE FROM panels WHERE id = ?", (panel_id,))
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)


db = Database()
