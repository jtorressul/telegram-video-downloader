"""TikTok: TikWM-compatible APIs → yt-dlp → Cobalt. All anonymous."""
import logging
import os
import re
import urllib.parse
from typing import Any, Dict

import config
from . import cobalt
from .base import Strategy
from .common import apply_id3_tags, convert_thumbnail_to_jpg, ensure_size, media_result, \
    carousel_result, to_mp3, ydl_download
from .errors import IPBlocked, NotFound, RegionOrAgeLocked, Unknown, Unsupported
from .net import Http, Route

logger = logging.getLogger(__name__)
PLATFORM, EMOJI = "TikTok", "🎵"
_SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com", "tiktok.com/t/")


def resolve_short_url(url: str) -> str:
    """Expands vm./vt. short links so every strategy gets the canonical /video/<id> URL."""
    if not any(h in url for h in _SHORT_HOSTS):
        return url
    try:
        with Http(timeout=12) as http:
            final = http.get(url, check=False).url
        if final and ('/video/' in final or '/photo/' in final):
            return final.split('?')[0]
    except Exception as e:
        logger.info(f"No se pudo expandir el enlace corto de TikTok: {e}")
    return url


def _tikwm(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    last_err: Exception = Unknown("sin endpoints TikWM")
    with Http(route, timeout=20) as http:
        for endpoint in config.TIKTOK_APIS:
            try:
                data = http.get_json(f"{endpoint}?url={urllib.parse.quote(url)}&hd=1")
            except Exception as e:
                last_err = e
                continue
            if data.get('code') == 0 and data.get('data'):
                return _from_tikwm(http, data['data'], format_type, workdir)
            msg = str(data.get('msg') or data)
            low = msg.lower()
            if 'limit' in low:
                last_err = IPBlocked(f"TikWM: {msg}")
            elif 'private' in low or 'not exist' in low or 'removed' in low:
                raise NotFound(f"TikWM: {msg}")
            else:
                last_err = Unknown(f"TikWM: {msg}")
    raise last_err


def _from_tikwm(http: Http, item: Dict[str, Any], format_type: str, workdir: str) -> Dict[str, Any]:
    title = item.get('title') or "Video de TikTok"
    author_obj = item.get('author') or {}
    author = author_obj.get('nickname') or author_obj.get('unique_id') or "TikTok"
    duration = item.get('duration') or 0
    vid_id = str(item.get('id') or 'tiktok')

    def thumb_for(media_path: str):
        if not item.get('cover'):
            return convert_thumbnail_to_jpg(None, media_path, workdir)
        try:
            raw = http.download(_abs(item['cover']), os.path.join(workdir, "cover.jpg"))
            return convert_thumbnail_to_jpg(raw, media_path, workdir)
        except Exception:
            return None

    if format_type == 'mp3':
        music_url = item.get('music') or item.get('play')
        if not music_url:
            raise Unsupported("No se encontró el audio de este TikTok.")
        raw = http.download(_abs(music_url), os.path.join(workdir, "audio_raw"))
        final = to_mp3(raw, os.path.join(workdir, f"{vid_id}.mp3"))
        thumb = thumb_for("")
        apply_id3_tags(final, title, author, thumb)
        return media_result('audio', final, PLATFORM, EMOJI, title, author, thumb, duration)

    images = item.get('images')
    if isinstance(images, list) and images:
        items = []
        for idx, img_url in enumerate(images[:10]):
            path = http.download(_abs(img_url), os.path.join(workdir, f"img_{idx}.jpg"))
            items.append(media_result('photo', path, PLATFORM, EMOJI, title, author))
        return carousel_result(items, PLATFORM, EMOJI, title, author)

    video_url = item.get('hdplay') or item.get('play')
    if not video_url:
        raise Unknown("TikWM no devolvió URL de video.")
    out = http.download(_abs(video_url), os.path.join(workdir, f"tiktok_{vid_id}.mp4"))
    res = media_result('video', out, PLATFORM, EMOJI, title, author, thumb_for(out), duration)
    ensure_size(res)
    return res


def _abs(u: str) -> str:
    return u if u.startswith('http') else f"https://www.tikwm.com{u}"


def _ytdlp(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    try:
        return ydl_download(url, format_type, workdir, route, PLATFORM, EMOJI)
    except Exception as e:
        if re.search(r'status code 10231', str(e)):
            raise RegionOrAgeLocked(str(e))
        raise


def _cobalt(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    return cobalt.download(url, format_type, workdir, PLATFORM, EMOJI)


STRATEGIES = [
    Strategy("tikwm", _tikwm),
    Strategy("ytdlp-tiktok", _ytdlp),
    Strategy("cobalt-tiktok", _cobalt, route_sensitive=False, enabled=cobalt.enabled),
]
