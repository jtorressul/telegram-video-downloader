import os
import sqlite3
import logging
import time
from datetime import datetime, date
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# DATA_DIR lets containers keep the databases on a persistent volume
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot_users.db")

NO_VIP_DAILY_LIMIT = 5
VIP_DAILY_LIMIT = 15

SOCIAL_PLATFORMS = {"Instagram", "TikTok", "X (Twitter)", "Facebook"}
HISTORY_KEEP = 50
LOG_RETENTION_DAYS = 30


def get_local_now() -> datetime:
    """Returns the current datetime in the local timezone (respecting TIMEZONE/TZ env vars)."""
    tz_name = os.getenv("TIMEZONE") or os.getenv("TZ")
    if tz_name:
        try:
            import zoneinfo
            return datetime.now(zoneinfo.ZoneInfo(tz_name.strip()))
        except Exception as e:
            logger.warning(f"Error loading timezone '{tz_name}': {e}. Using system local timezone.")
    return datetime.now().astimezone()


def get_local_today_str() -> str:
    """Returns today's date formatted as YYYY-MM-DD in the local timezone."""
    return get_local_now().date().isoformat()


class UserDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes users and groups tables."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_vip INTEGER DEFAULT 0,
                    total_downloads INTEGER DEFAULT 0,
                    social_downloads INTEGER DEFAULT 0,
                    other_downloads INTEGER DEFAULT 0,
                    daily_downloads INTEGER DEFAULT 0,
                    last_download_date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            try:
                # Unix time of the last VIP verification against the groups
                conn.execute("ALTER TABLE users ADD COLUMN vip_checked_at REAL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS download_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    format TEXT NOT NULL,
                    platform TEXT,
                    title TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON download_history (user_id, id)")
            # One row per delivery attempt, for admin stats (/estado, /top); pruned after LOG_RETENTION_DAYS
            conn.execute("""
                CREATE TABLE IF NOT EXISTS download_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    platform TEXT,
                    cached INTEGER DEFAULT 0,
                    ok INTEGER DEFAULT 1,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_log_day ON download_log (day)")
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_groups (
                    group_id INTEGER PRIMARY KEY,
                    title TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def register_group(self, group_id: int, title: Optional[str] = None):
        """Registers or updates a known group."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO known_groups (group_id, title, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_id) DO UPDATE SET title = excluded.title, updated_at = CURRENT_TIMESTAMP
            """, (group_id, title or ""))
            conn.commit()

    def get_known_groups(self) -> list:
        """Returns list of known group IDs."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT group_id FROM known_groups")
            return [row[0] for row in cur.fetchall()]

    def reset_all_daily_quotas(self) -> int:
        """
        Resets daily download counters to 0 for all users at 12:00 AM local time.
        Returns the number of user records updated.
        """
        today_str = get_local_today_str()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET daily_downloads = 0, last_download_date = ?", (today_str,))
            conn.commit()
            count = cur.rowcount
            logger.info(f"Reinicio de cuotas medianoche ejecutado para {count} usuarios (Fecha local: {today_str})")
            return count

    def get_or_create_user(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
        """Fetches or registers a user, handling daily quota resets."""
        today_str = get_local_today_str()

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()

            if not row:
                conn.execute("""
                    INSERT INTO users (user_id, username, first_name, last_download_date)
                    VALUES (?, ?, ?, ?)
                """, (user_id, username or "", first_name or "", today_str))
                conn.commit()
                cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
            else:
                updates = []
                params = []
                if username and row['username'] != username:
                    updates.append("username = ?")
                    params.append(username)
                if first_name and row['first_name'] != first_name:
                    updates.append("first_name = ?")
                    params.append(first_name)

                if row['last_download_date'] != today_str:
                    updates.append("daily_downloads = 0")
                    updates.append("last_download_date = ?")
                    params.append(today_str)

                if updates:
                    params.append(user_id)
                    conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", tuple(params))
                    conn.commit()
                    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                    row = cur.fetchone()

            return dict(row)

    def set_vip_status(self, user_id: int, is_vip: bool, username: Optional[str] = None, first_name: Optional[str] = None):
        """Sets VIP status (1 or 0) for a user and stamps the verification time."""
        self.get_or_create_user(user_id, username, first_name)
        with self._get_connection() as conn:
            conn.execute("UPDATE users SET is_vip = ?, vip_checked_at = ? WHERE user_id = ?",
                         (1 if is_vip else 0, time.time(), user_id))
            conn.commit()

    def get_vip_user_ids(self) -> list:
        with self._get_connection() as conn:
            return [row[0] for row in conn.execute("SELECT user_id FROM users WHERE is_vip = 1")]

    def check_download_permission(self, user_id: int, platform: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates if user can download given their VIP status and quota.
        Returns (allowed: bool, reason: str, user_dict: dict).
        Reasons: 'ok', 'platform_restricted', 'daily_limit_reached'.
        """
        user = self.get_or_create_user(user_id)
        is_vip = bool(user.get('is_vip', 0))
        today_str = get_local_today_str()
        daily_used = user.get('daily_downloads', 0)
        if user.get('last_download_date') != today_str:
            daily_used = 0

        # 1. Platform check: NO VIP only allowed SOCIAL_PLATFORMS
        if not is_vip and platform not in SOCIAL_PLATFORMS:
            return False, "platform_restricted", user

        # 2. Daily limit check
        max_daily = VIP_DAILY_LIMIT if is_vip else NO_VIP_DAILY_LIMIT
        if daily_used >= max_daily:
            return False, "daily_limit_reached", user

        return True, "ok", user

    def record_download_success(self, user_id: int, platform: str, count_quota: bool = True):
        """Increments download stats and, unless count_quota is False (cache hits), the daily quota."""
        today_str = get_local_today_str()
        is_social = 1 if platform in SOCIAL_PLATFORMS else 0

        with self._get_connection() as conn:
            user = self.get_or_create_user(user_id)
            daily_used = user.get('daily_downloads', 0)
            if user.get('last_download_date') != today_str:
                daily_used = 0

            conn.execute("""
                UPDATE users SET
                    total_downloads = total_downloads + 1,
                    social_downloads = social_downloads + ?,
                    other_downloads = other_downloads + ?,
                    daily_downloads = ?,
                    last_download_date = ?
                WHERE user_id = ?
            """, (
                1 if is_social else 0,
                0 if is_social else 1,
                daily_used + (1 if count_quota else 0),
                today_str,
                user_id
            ))
            conn.commit()

    def add_history(self, user_id: int, url: str, format_type: str, platform: str, title: Optional[str]):
        """Stores a download, keeping only the most recent HISTORY_KEEP per user."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO download_history (user_id, url, format, platform, title) VALUES (?, ?, ?, ?, ?)
            """, (user_id, url, format_type, platform, (title or "")[:200]))
            conn.execute("""
                DELETE FROM download_history WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM download_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)
            """, (user_id, user_id, HISTORY_KEEP))
            conn.commit()

    def get_history(self, user_id: int, limit: int = 10) -> list:
        """Most recent downloads first, one entry per (url, format)."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT url, format, platform, title, MAX(id) AS last_id FROM download_history
                WHERE user_id = ? GROUP BY url, format ORDER BY last_id DESC LIMIT ?
            """, (user_id, limit)).fetchall()
            return [dict(r) for r in rows]

    # --- Admin: activity log ---------------------------------------------------------------
    def log_download(self, user_id: int, platform: str, ok: bool, cached: bool = False):
        with self._get_connection() as conn:
            conn.execute("INSERT INTO download_log (day, user_id, platform, cached, ok, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (get_local_today_str(), user_id, platform, int(cached), int(ok), time.time()))
            conn.commit()

    def prune_download_log(self) -> int:
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM download_log WHERE created_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    def platform_stats(self, day: str) -> list:
        """[{platform, ok, cached, failed}] for one local day, busiest first."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT platform, SUM(ok) AS ok, SUM(ok AND cached) AS cached, SUM(1 - ok) AS failed
                FROM download_log WHERE day = ? GROUP BY platform ORDER BY COUNT(*) DESC
            """, (day,)).fetchall()
            return [dict(r) for r in rows]

    def top_users(self, since_ts: float, limit: int = 10) -> list:
        """[{user_id, username, first_name, downloads}] by successful downloads since since_ts."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT l.user_id, u.username, u.first_name, COUNT(*) AS downloads
                FROM download_log l LEFT JOIN users u ON u.user_id = l.user_id
                WHERE l.ok = 1 AND l.created_at >= ?
                GROUP BY l.user_id ORDER BY downloads DESC LIMIT ?
            """, (since_ts, limit)).fetchall()
            return [dict(r) for r in rows]

    def top_platforms(self, since_ts: float) -> list:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT platform, COUNT(*) AS downloads FROM download_log
                WHERE ok = 1 AND created_at >= ? GROUP BY platform ORDER BY downloads DESC
            """, (since_ts,)).fetchall()
            return [dict(r) for r in rows]

    def user_counts(self) -> Dict[str, int]:
        today = get_local_today_str()
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(is_vip = 1) AS vip,
                       SUM(is_banned = 1) AS banned,
                       SUM(last_download_date = ? AND daily_downloads > 0) AS active_today
                FROM users
            """, (today,)).fetchone()
            return {k: row[k] or 0 for k in ("total", "vip", "banned", "active_today")}

    # --- Admin: VIP list / bans / broadcast ---------------------------------------------------
    def get_vip_users(self) -> list:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT user_id, username, first_name, vip_checked_at FROM users "
                                "WHERE is_vip = 1 ORDER BY vip_checked_at DESC").fetchall()
            return [dict(r) for r in rows]

    def set_banned(self, user_id: int, banned: bool):
        self.get_or_create_user(user_id)
        with self._get_connection() as conn:
            conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(banned), user_id))
            conn.commit()

    def is_banned(self, user_id: int) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return bool(row and row[0])

    def get_broadcast_user_ids(self) -> list:
        with self._get_connection() as conn:
            return [r[0] for r in conn.execute("SELECT user_id FROM users WHERE COALESCE(is_banned, 0) = 0")]

    def get_quota(self, user_id: int) -> Tuple[int, int]:
        """Returns (used_today, daily_limit) for the user."""
        user = self.get_or_create_user(user_id)
        max_daily = VIP_DAILY_LIMIT if user.get('is_vip') else NO_VIP_DAILY_LIMIT
        return user.get('daily_downloads', 0), max_daily

    def get_stats_message(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> str:
        """Generates formatted user statistics card."""
        user = self.get_or_create_user(user_id, username, first_name)
        is_vip = bool(user.get('is_vip', 0))

        status_text = "VIP" if is_vip else "NO VIP PASS"
        total = user.get('total_downloads', 0)
        social = user.get('social_downloads', 0)
        other = user.get('other_downloads', 0)
        today_str = get_local_today_str()
        daily = user.get('daily_downloads', 0)
        if user.get('last_download_date') != today_str:
            daily = 0
        max_daily = VIP_DAILY_LIMIT if is_vip else NO_VIP_DAILY_LIMIT

        return (
            "📊 <b>Tus Estadísticas</b>\n\n"
            f"💎 <b>Estado:</b> {status_text}\n"
            f"📥 <b>Descargas totales:</b> {total}\n"
            f"📱 <b>Social:</b> {social} | 🌐 <b>Otras:</b> {other}\n"
            f"🗓 <b>Cuota diaria:</b> {daily}/{max_daily}"
        )
