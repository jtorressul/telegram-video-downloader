"""Anonymous media downloaders (TikTok, YouTube, Instagram, X, Spotify, Facebook and generic sites).

Public API kept compatible with the old downloader.py module.
"""
import asyncio
import logging
import shutil
import tempfile
from typing import Any, Dict, Optional

from . import facebook, instagram, spotify, tiktok, twitter, youtube
from .base import run_chain, new_workdir
from .common import (
    MAX_TELEGRAM_SIZE_BYTES,
    detect_platform,
    extract_youtube_id,
    format_duration,
    is_spotify_url,
    is_youtube_url,
)
from .errors import DownloadError
from .instagram import normalize_instagram_url

logger = logging.getLogger(__name__)

__all__ = [
    "VideoDownloader", "DownloadError", "MAX_TELEGRAM_SIZE_BYTES", "detect_platform",
    "extract_youtube_id", "format_duration", "is_spotify_url", "is_youtube_url", "normalize_instagram_url",
]


def strategies_for(platform: str, emoji: str):
    if platform == "Spotify":
        return spotify.STRATEGIES
    if platform == "TikTok":
        return tiktok.STRATEGIES
    if platform == "X (Twitter)":
        return twitter.STRATEGIES
    if platform == "Instagram":
        return instagram.STRATEGIES
    if platform in ("YouTube", "YouTube Music"):
        return youtube.STRATEGIES
    if platform == "Facebook":
        return facebook.FACEBOOK_STRATEGIES
    return facebook.generic_strategies(platform, emoji)


def prepare_url(url: str, platform: str) -> str:
    if platform == "TikTok":
        return tiktok.resolve_short_url(url)
    if platform == "Instagram":
        return normalize_instagram_url(url)
    return url


class VideoDownloader:
    def __init__(self, temp_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        # cookies_file kept for signature compatibility; cookies are read via config.resolve_cookies_file()
        self.temp_dir = temp_dir or tempfile.gettempdir()

    def _sync_download(self, url: str, format_type: str, workdir: str) -> Dict[str, Any]:
        format_type = format_type.lower().strip()
        if format_type not in ('mp3', 'mp4'):
            format_type = 'mp4'
        platform, emoji = detect_platform(url)
        if platform == "Spotify":
            format_type = 'mp3'
        url = prepare_url(url, platform)
        return run_chain(platform, strategies_for(platform, emoji), url, format_type, workdir)

    async def download(self, url: str, format_type: str = "mp4") -> Dict[str, Any]:
        """Downloads media into an isolated temporary directory (cleaned up via cleanup())."""
        workdir = new_workdir(self.temp_dir)
        try:
            result = await asyncio.to_thread(self._sync_download, url, format_type, workdir)
            result['temp_dir'] = workdir
            return result
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    @staticmethod
    def cleanup(download_result: Dict[str, Any]) -> None:
        temp_dir = download_result.get('temp_dir')
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
