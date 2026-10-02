"""Facebook (yt-dlp for videos, OpenGraph for photos) and the generic yt-dlp fallback for other sites."""
import html
import re
from typing import Any, Dict

from .base import Strategy
from .common import download_media_list, ydl_download
from .errors import Private, Unsupported
from .net import Http, Route

PLATFORM, EMOJI = "Facebook", "👥"


def _ytdlp_fb(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    is_video_hint = any(k in url.lower() for k in ['/reel/', '/watch', 'fb.watch', '/videos/', '/share/v/', '/share/r/'])
    if not is_video_hint and format_type != 'mp3':
        raise Unsupported("no parece un video; se intenta como foto")
    return ydl_download(url, format_type, workdir, route, PLATFORM, EMOJI,
                        {'format': 'bestaudio/best' if format_type == 'mp3' else 'best[ext=mp4]/best'})


def _meta(page: str, prop: str):
    m = re.search(rf'["\']{prop}["\']\s*content=["\']([^"\']+)["\']', page) or \
        re.search(rf'content=["\']([^"\']+)["\']\s*property=["\']{prop}["\']', page)
    return html.unescape(m.group(1)) if m else None


def _opengraph_fb(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    if format_type == 'mp3':
        raise Unsupported("OpenGraph solo obtiene fotos")
    with Http(route, timeout=15) as http:
        page = http.get(url, headers={'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8'}).text
        img = _meta(page, 'og:image') or _meta(page, 'twitter:image')
        if not img:
            raise Private("Facebook no expone esta publicación sin sesión")
        title = _meta(page, 'og:title') or "Foto de Facebook"
        desc = _meta(page, 'og:description') or ""
        if desc and title == "Foto de Facebook":
            title = desc[:200]
        return download_media_list(http, [{'type': 'photo', 'url': img}], format_type, workdir, 'fb',
                                   PLATFORM, EMOJI, title, "Facebook")


def _generic(platform: str, emoji: str):
    def run(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
        return ydl_download(url, format_type, workdir, route, platform, emoji)
    return run


FACEBOOK_STRATEGIES = [
    Strategy("ytdlp-fb", _ytdlp_fb),
    Strategy("fb-opengraph", _opengraph_fb),
]


def generic_strategies(platform: str, emoji: str):
    return [Strategy(f"ytdlp-{platform.split(' ')[0].lower()}", _generic(platform, emoji))]
