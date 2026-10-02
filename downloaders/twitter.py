"""X / Twitter: fxtwitter → vxtwitter → syndication API → yt-dlp. All anonymous."""
import math
import re
from typing import Any, Dict, List, Tuple

from .base import Strategy
from .common import download_media_list, ydl_download
from .errors import NotFound, Unknown, Unsupported
from .net import DIRECT, Http, Route

PLATFORM, EMOJI = "X (Twitter)", "🐦"
_BASE36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def parse_tweet_url(url: str) -> Tuple[str, str]:
    m = re.search(r'(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com)/'
                  r'(?:([a-zA-Z0-9_]+)/status(?:es)?/|i/(?:web/)?status/)(\d+)', url)
    if not m:
        raise Unsupported("URL de X (Twitter) no válida.")
    return m.group(1) or "i", m.group(2)


def _download(media, format_type, workdir, twid, text, author) -> Dict[str, Any]:
    # Twitter's CDN serves media to any IP, so a direct session is enough
    with Http(DIRECT, timeout=30) as http:
        return download_media_list(http, media, format_type, workdir, f"tw_{twid}", PLATFORM, EMOJI,
                                   text, author)


def _fxtwitter(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    user, twid = parse_tweet_url(url)
    with Http(route, timeout=15) as http:
        resp = http.get(f"https://api.fxtwitter.com/{user}/status/{twid}", check=False)
    if resp.status_code == 404:
        raise NotFound("fxtwitter 404")
    if resp.status_code >= 400:
        raise Unknown(f"fxtwitter HTTP {resp.status_code}")
    tweet = (resp.json() or {}).get('tweet') or {}
    text = tweet.get('text') or "Tweet de X"
    author = (tweet.get('author') or {}).get('name') or user
    media_obj = tweet.get('media') or {}
    raw = media_obj.get('all') or ((media_obj.get('videos') or []) + (media_obj.get('photos') or []))
    media = [{
        'type': 'gif' if m.get('type') == 'gif' else ('video' if m.get('type') == 'video' else 'photo'),
        'url': m.get('url'),
        'thumbnail_url': m.get('thumbnail_url'),
        'duration': m.get('duration'),
        'width': m.get('width'),
        'height': m.get('height'),
    } for m in raw]
    return _download(media, format_type, workdir, twid, text, author)


def _vxtwitter(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    user, twid = parse_tweet_url(url)
    with Http(route, timeout=15) as http:
        data = http.get_json(f"https://api.vxtwitter.com/{user}/status/{twid}")
    text = data.get('text') or "Tweet de X"
    author = data.get('user_name') or data.get('user_screen_name') or user
    media = []
    for m in data.get('media_extended') or []:
        media.append({
            'type': 'gif' if m.get('type') == 'gif' else ('video' if m.get('type') == 'video' else 'photo'),
            'url': m.get('url'),
            'thumbnail_url': m.get('thumbnail_url'),
            'duration': (m.get('duration_millis') or 0) / 1000 or None,
            'width': (m.get('size') or {}).get('width'),
            'height': (m.get('size') or {}).get('height'),
        })
    return _download(media, format_type, workdir, twid, text, author)


def syndication_token(tweet_id: str) -> str:
    """Port of the token used by Twitter's embed widget: ((id / 1e15) * PI).toString(36)."""
    x = (int(tweet_id) / 1e15) * math.pi
    int_part = int(x)
    frac = x - int_part
    digits = ''
    n = int_part
    while n:
        n, r = divmod(n, 36)
        digits = _BASE36[r] + digits
    frac_digits = ''
    for _ in range(12):
        frac *= 36
        d = int(frac)
        frac_digits += _BASE36[d]
        frac -= d
    return re.sub(r'(0+|\.)', '', f"{digits or '0'}.{frac_digits}")


def _syndication(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    _, twid = parse_tweet_url(url)
    with Http(route, timeout=15) as http:
        resp = http.get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={twid}&lang=en"
            f"&token={syndication_token(twid)}", check=False)
    if resp.status_code == 404 or not resp.text.strip():
        raise NotFound("syndication: tweet no encontrado")
    if resp.status_code >= 400:
        raise Unknown(f"syndication HTTP {resp.status_code}")
    data = resp.json()
    text = data.get('text') or "Tweet de X"
    author = (data.get('user') or {}).get('name') or "X"
    media = [_syndication_media(m) for m in data.get('mediaDetails') or []]
    return _download([m for m in media if m.get('url')], format_type, workdir, twid, text, author)


def _syndication_media(m: Dict[str, Any]) -> Dict[str, Any]:
    if m.get('type') in ('video', 'animated_gif'):
        variants: List[Dict[str, Any]] = [v for v in (m.get('video_info') or {}).get('variants', [])
                                          if v.get('content_type') == 'video/mp4']
        best = max(variants, key=lambda v: v.get('bitrate') or 0) if variants else {}
        return {
            'type': 'gif' if m.get('type') == 'animated_gif' else 'video',
            'url': best.get('url'),
            'thumbnail_url': m.get('media_url_https'),
            'duration': ((m.get('video_info') or {}).get('duration_millis') or 0) / 1000 or None,
        }
    return {'type': 'photo', 'url': m.get('media_url_https')}


def _ytdlp(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    return ydl_download(url, format_type, workdir, route, PLATFORM, EMOJI)


STRATEGIES = [
    Strategy("fxtwitter", _fxtwitter, route_sensitive=False),
    Strategy("vxtwitter", _vxtwitter, route_sensitive=False),
    Strategy("syndication", _syndication),
    Strategy("ytdlp-x", _ytdlp),
]
