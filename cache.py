import os
import sqlite3
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qs

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "cache.db")


def normalize_url(url: str) -> str:
    """
    Strips tracking queries (igsh, si, utm_*, fbclid, etc.)
    to ensure matching cached videos even if shared with different tracking tags.
    """
    try:
        parsed = urlparse(url.strip())
        clean_netloc = parsed.netloc.lower()
        clean_path = parsed.path.rstrip('/')

        if 'youtube.com' in clean_netloc and '/watch' in clean_path:
            qs = parse_qs(parsed.query)
            v = qs.get('v', [''])[0]
            clean_query = f"v={v}" if v else ""
        else:
            clean_query = ""

        return urlunparse((parsed.scheme, clean_netloc, clean_path, '', clean_query, ''))
    except Exception:
        return url.strip()


class VideoCache:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes the SQLite cache table."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS video_cache (
                    url_key TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    title TEXT,
                    platform TEXT,
                    duration INTEGER,
                    width INTEGER,
                    height INTEGER,
                    filesize INTEGER,
                    is_audio INTEGER DEFAULT 0,
                    performer TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Safe migration for existing DB
            try:
                conn.execute("ALTER TABLE video_cache ADD COLUMN is_audio INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE video_cache ADD COLUMN performer TEXT")
            except Exception:
                pass
            conn.commit()

    def get(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves cached video/audio metadata and Telegram file_id by URL.
        """
        key = normalize_url(url)
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT file_id, title, platform, duration, width, height, filesize, is_audio, performer FROM video_cache WHERE url_key = ?",
                    (key,)
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
        except Exception as e:
            logger.warning(f"Error reading cache for {url}: {e}")
        return None

    def set(self, url: str, file_id: str, title: str, platform: str, duration: Optional[int], width: Optional[int], height: Optional[int], filesize: int, is_audio: bool = False, performer: Optional[str] = None):
        """
        Saves Telegram file_id and metadata for a video/audio URL.
        """
        key = normalize_url(url)
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO video_cache (url_key, file_id, title, platform, duration, width, height, filesize, is_audio, performer)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (key, file_id, title, platform, duration, width, height, filesize, 1 if is_audio else 0, performer))
                conn.commit()
        except Exception as e:
            logger.warning(f"Error saving to cache for {url}: {e}")
