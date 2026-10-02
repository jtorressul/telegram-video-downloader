import os
import sqlite3
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qs

logger = logging.getLogger(__name__)

# DATA_DIR lets containers keep the databases on a persistent volume
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "cache.db")


def normalize_url(url: str) -> str:
    """Strips tracking queries to ensure clean URL keys."""
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
                    media_type TEXT DEFAULT 'video',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Short numeric ids for URLs: Telegram button data is limited to 64 bytes
            conn.execute("""
                CREATE TABLE IF NOT EXISTS url_refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL
                )
            """)
            try:
                conn.execute("ALTER TABLE video_cache ADD COLUMN is_audio INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE video_cache ADD COLUMN performer TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE video_cache ADD COLUMN media_type TEXT DEFAULT 'video'")
            except Exception:
                pass
            conn.commit()

    def _make_key(self, video_id_or_url: str, format_type: str = "mp4") -> str:
        fmt = format_type.lower().strip()
        if len(video_id_or_url) == 11 and not ('/' in video_id_or_url or '.' in video_id_or_url):
            return f"{video_id_or_url}:{fmt}"
        clean = normalize_url(video_id_or_url)
        return f"{clean}:{fmt}"

    def get(self, video_id_or_url: str, format_type: str = "mp4") -> Optional[Dict[str, Any]]:
        """Retrieves cached metadata and Telegram file_id."""
        key = self._make_key(video_id_or_url, format_type)
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT file_id, title, platform, duration, width, height, filesize, is_audio, performer, media_type FROM video_cache WHERE url_key = ?",
                    (key,)
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
        except Exception as e:
            logger.warning(f"Error reading cache for {video_id_or_url}: {e}")
        return None

    def set(
        self,
        video_id_or_url: str,
        format_type: str,
        file_id: str,
        title: str,
        platform: str,
        duration: Optional[int],
        width: Optional[int],
        height: Optional[int],
        filesize: int,
        is_audio: bool = False,
        performer: Optional[str] = None,
        media_type: str = "video",
    ):
        """Saves Telegram file_id and metadata for media in specific format."""
        key = self._make_key(video_id_or_url, format_type)
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO video_cache (url_key, file_id, title, platform, duration, width, height, filesize, is_audio, performer, media_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (key, file_id, title, platform, duration, width, height, filesize, 1 if is_audio else 0, performer, media_type))
                conn.commit()
        except Exception as e:
            logger.warning(f"Error saving to cache for {video_id_or_url}: {e}")

    def url_ref(self, url: str) -> int:
        """Returns a stable short id for url (for inline button callback data)."""
        with self._get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO url_refs (url) VALUES (?)", (url,))
            conn.commit()
            return conn.execute("SELECT id FROM url_refs WHERE url = ?", (url,)).fetchone()[0]

    def url_from_ref(self, ref: int) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT url FROM url_refs WHERE id = ?", (ref,)).fetchone()
            return row[0] if row else None
