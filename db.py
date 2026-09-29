import os
import sqlite3
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "bot_users.db")

NO_VIP_DAILY_LIMIT = 10
VIP_DAILY_LIMIT = 20

SOCIAL_PLATFORMS = {"Instagram", "TikTok", "X (Twitter)"}


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

    def get_or_create_user(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
        """Fetches or registers a user, handling daily quota resets."""
        today_str = date.today().isoformat()

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
                # Update username or first_name if changed
                updates = []
                params = []
                if username and row['username'] != username:
                    updates.append("username = ?")
                    params.append(username)
                if first_name and row['first_name'] != first_name:
                    updates.append("first_name = ?")
                    params.append(first_name)

                # Daily quota reset if date changed
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
        """Sets VIP status (1 or 0) for a user, ensuring the user exists first."""
        self.get_or_create_user(user_id, username, first_name)
        with self._get_connection() as conn:
            conn.execute("UPDATE users SET is_vip = ? WHERE user_id = ?", (1 if is_vip else 0, user_id))
            conn.commit()

    def check_download_permission(self, user_id: int, platform: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates if user can download given their VIP status and quota.
        Returns (allowed: bool, reason: str, user_dict: dict).
        Reasons: 'ok', 'platform_restricted', 'daily_limit_reached'.
        """
        user = self.get_or_create_user(user_id)
        is_vip = bool(user.get('is_vip', 0))
        daily_used = user.get('daily_downloads', 0)

        # 1. Platform check: NO VIP only allowed X, Instagram, TikTok
        if not is_vip and platform not in SOCIAL_PLATFORMS:
            return False, "platform_restricted", user

        # 2. Daily limit check
        max_daily = VIP_DAILY_LIMIT if is_vip else NO_VIP_DAILY_LIMIT
        if daily_used >= max_daily:
            return False, "daily_limit_reached", user

        return True, "ok", user

    def record_download_success(self, user_id: int, platform: str):
        """Increments download stats and daily quota."""
        today_str = date.today().isoformat()
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
                daily_used + 1,
                today_str,
                user_id
            ))
            conn.commit()

    def get_stats_message(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> str:
        """Generates formatted user statistics card."""
        user = self.get_or_create_user(user_id, username, first_name)
        is_vip = bool(user.get('is_vip', 0))

        status_text = "VIP" if is_vip else "NO VIP PASS"
        total = user.get('total_downloads', 0)
        social = user.get('social_downloads', 0)
        other = user.get('other_downloads', 0)
        daily = user.get('daily_downloads', 0)
        max_daily = VIP_DAILY_LIMIT if is_vip else NO_VIP_DAILY_LIMIT

        return (
            "📊 <b>Tus Estadísticas</b>\n\n"
            f"💎 <b>Estado:</b> {status_text}\n"
            f"📥 <b>Descargas:</b> {total}\n"
            f"📱 <b>Social:</b> {social} | 🌐 <b>Otras:</b> {other}\n"
            f"🗓 <b>Cuota diaria:</b> {daily}/{max_daily}"
        )
