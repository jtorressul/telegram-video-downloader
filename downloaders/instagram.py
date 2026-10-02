"""Instagram: Polaris GraphQL → embed page → yt-dlp → Cobalt → OpenGraph. All anonymous.

instagrapi (account login) remains only as an opt-in strategy for ANONYMOUS_ONLY=false.
Logging in from a datacenter IP is what got the service account suspended.
"""
import html
import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

import config
from . import cobalt
from .base import Strategy
from .common import download_media_list, ydl_download
from .errors import IPBlocked, NotFound, Private, RegionOrAgeLocked, Unknown, Unsupported
from .net import Http, Route

logger = logging.getLogger(__name__)
PLATFORM, EMOJI = "Instagram", "📸"
_SC_RE = r'(?:instagram\.com|instagr\.am|ig\.me)/(?:[A-Za-z0-9_.]+/)?(p|reel|reels|tv|share/reel|share/p)/([a-zA-Z0-9_-]+)'
_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
_IG_HEADERS = {'Referer': 'https://www.instagram.com/'}


def normalize_instagram_url(url: str) -> str:
    m = re.search(_SC_RE, url)
    if m:
        kind = 'p' if m.group(1).lower() in ('p', 'share/p') else 'reel'
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    return url


def extract_instagram_shortcode(url: str) -> Optional[str]:
    m = re.search(_SC_RE, url)
    return m.group(2) if m else None


def _shortcode(url: str) -> str:
    sc = extract_instagram_shortcode(url)
    if not sc:
        raise Unsupported("URL de Instagram no válida (usa enlaces de /p/ o /reel/).")
    return sc


def _download(media, format_type, workdir, sc, caption, author, route):
    with Http(route, timeout=60) as http:
        return download_media_list(http, media, format_type, workdir, f"ig_{sc}", PLATFORM, EMOJI,
                                   caption or "Publicación de Instagram", author or "Instagram",
                                   headers=_IG_HEADERS)


# ------------------------------------------------------------------------------
# Normalizers
# ------------------------------------------------------------------------------
def media_from_api_item(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Private-API style item (media_type 1/2/8) → normalized media list."""
    def one(node):
        imgs = (node.get('image_versions2') or {}).get('candidates') or []
        thumb = imgs[0].get('url') if imgs else None
        if node.get('media_type') == 2 and node.get('video_versions'):
            v = node['video_versions'][0]
            return {'type': 'video', 'url': v.get('url'), 'thumbnail_url': thumb,
                    'duration': node.get('video_duration'), 'width': v.get('width'), 'height': v.get('height')}
        if imgs:
            return {'type': 'photo', 'url': thumb, 'width': imgs[0].get('width'), 'height': imgs[0].get('height')}
        return None

    nodes = item.get('carousel_media') if item.get('media_type') == 8 else [item]
    return [m for m in (one(n) for n in nodes or []) if m and m.get('url')]


def media_from_graphql(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Public web GraphQL shortcode_media (GraphVideo / GraphImage / GraphSidecar) → media list."""
    def one(n):
        if n.get('is_video') and n.get('video_url'):
            dims = n.get('dimensions') or {}
            return {'type': 'video', 'url': n['video_url'], 'thumbnail_url': n.get('display_url'),
                    'duration': n.get('video_duration'), 'width': dims.get('width'), 'height': dims.get('height')}
        if n.get('display_url'):
            return {'type': 'photo', 'url': n['display_url']}
        return None

    children = ((node.get('edge_sidecar_to_children') or {}).get('edges')) or []
    nodes = [c.get('node') or {} for c in children] or [node]
    return [m for m in (one(n) for n in nodes) if m]


def graphql_caption(node: Dict[str, Any]) -> str:
    edges = (node.get('edge_media_to_caption') or {}).get('edges') or []
    return ((edges[0].get('node') or {}).get('text') if edges else '') or "Publicación de Instagram"


# ------------------------------------------------------------------------------
# Strategies
# ------------------------------------------------------------------------------
def _polaris(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    sc = _shortcode(url)
    media_id = 0
    for ch in sc:
        media_id = media_id * 64 + _ALPHABET.index(ch)
    with Http(route, timeout=15) as http:
        home = http.get("https://www.instagram.com/")
        csrf = http.session.cookies.get("csrftoken", "")
        lsd_m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"\}', home.text)
        lsd = lsd_m.group(1) if lsd_m else "AVr"
        resp = http.post("https://www.instagram.com/api/graphql", headers={
            'X-IG-App-ID': '936619743392459',
            'X-FB-Friendly-Name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
            'X-CSRFToken': csrf,
            'X-FB-LSD': lsd,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'https://www.instagram.com/reel/{sc}/',
        }, data={
            'lsd': lsd,
            'fb_api_caller_class': 'RelayModern',
            'fb_api_req_friendly_name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
            'server_timestamps': 'true',
            'variables': json.dumps({'media_id': str(media_id)}, separators=(',', ':')),
            'doc_id': '28256812867323632',
        })
    data = resp.json() if resp.text.strip().startswith('{') else {}
    node = (data.get('data') or {}).get('xig_polaris_media') or {}
    item = node.get('if_not_gated_logged_out')
    if not item:
        if node:
            # Meta returned the post but gated it for logged-out viewers (age / sensitive content)
            raise RegionOrAgeLocked("Polaris: contenido restringido para visitantes sin sesión")
        raise Unknown(f"Polaris sin datos: {str(data)[:150]}")
    caption = (item.get('caption') or {}).get('text')
    author = (item.get('user') or {}).get('username')
    return _download(media_from_api_item(item), format_type, workdir, sc, caption, author, route)


def parse_embed_html(page: str) -> Optional[Dict[str, Any]]:
    """Extracts {'media', 'caption', 'author'} from the /embed/captioned/ page."""
    m = re.search(r'"contextJSON":("(?:[^"\\]|\\.)*")', page)
    if m:
        try:
            ctx = json.loads(json.loads(m.group(1)))
            node = ((ctx.get('gql_data') or {}).get('shortcode_media')) or {}
            if node:
                return {
                    'media': media_from_graphql(node),
                    'caption': graphql_caption(node),
                    'author': (node.get('owner') or {}).get('username'),
                }
        except (ValueError, TypeError) as e:
            logger.debug(f"contextJSON no parseable: {e}")

    vid = re.search(r'"video_url":"([^"]+)"', page)
    img = re.search(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"', page) or \
        re.search(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', page)
    author_m = re.search(r'class="UsernameText"[^>]*>([^<]+)<', page)
    media = []
    if vid:
        media.append({'type': 'video', 'url': json.loads(f'"{vid.group(1)}"'),
                      'thumbnail_url': html.unescape(img.group(1)) if img else None})
    elif img:
        media.append({'type': 'photo', 'url': html.unescape(img.group(1))})
    if not media:
        return None
    return {'media': media, 'caption': "Publicación de Instagram",
            'author': author_m.group(1).strip() if author_m else None}


def _embed(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    sc = _shortcode(url)
    kind = 'reel' if '/reel' in url else 'p'
    with Http(route, timeout=15) as http:
        resp = http.get(f"https://www.instagram.com/{kind}/{sc}/embed/captioned/", check=False)
    if resp.status_code == 404:
        raise NotFound("embed 404")
    if resp.status_code >= 400:
        raise IPBlocked(f"embed HTTP {resp.status_code}")
    parsed = parse_embed_html(resp.text)
    if not parsed:
        if 'EmbedIsBroken' in resp.text:
            raise Private("embed no disponible (privado o embeds desactivados)")
        # No media: deleted post, logged-out gate or embeds disabled; other strategies decide
        raise Unknown("embed sin medios")
    if format_type == 'mp3' and not any(m['type'] == 'video' for m in parsed['media']):
        raise Unknown("embed sin video (puede ser un carrusel incompleto)")
    return _download(parsed['media'], format_type, workdir, sc, parsed['caption'], parsed['author'], route)


def _ytdlp(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    return ydl_download(url, format_type, workdir, route, PLATFORM, EMOJI)


def _cobalt(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    return cobalt.download(url, format_type, workdir, PLATFORM, EMOJI)


def _opengraph(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    if format_type == 'mp3':
        raise Unsupported("OpenGraph solo obtiene fotos")
    sc = _shortcode(url)
    with Http(route, timeout=15) as http:
        page = http.get(url).text
    img = re.search(r'property="og:image"\s+content="([^"]+)"', page) or \
        re.search(r'content="([^"]+)"\s+property="og:image"', page)
    if not img:
        raise Unknown("sin og:image")
    title = re.search(r'property="og:title"\s+content="([^"]+)"', page)
    return _download([{'type': 'photo', 'url': html.unescape(img.group(1))}], format_type, workdir, sc,
                     html.unescape(title.group(1)) if title else None, None, route)


# --- Opt-in, login based (disabled while ANONYMOUS_ONLY=true) ---
_instagrapi_client = None


def _instagrapi_enabled() -> bool:
    return bool(config.INSTAGRAM_SESSIONID or (config.IG_USERNAME and config.IG_PASSWORD))


def _instagrapi(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    global _instagrapi_client
    if _instagrapi_client is None:
        from instagrapi import Client
        cl = Client()
        session_file = os.path.join(tempfile.gettempdir(), "instagrapi_session.json")
        if os.path.exists(session_file):
            cl.load_settings(session_file)
        if config.INSTAGRAM_SESSIONID:
            cl.login_by_sessionid(config.INSTAGRAM_SESSIONID)
        else:
            cl.login(config.IG_USERNAME, config.IG_PASSWORD)
        cl.dump_settings(session_file)
        _instagrapi_client = cl
    cl = _instagrapi_client
    pk = cl.media_pk_from_url(url)
    raw = cl.private_request(f"media/{pk}/info/")
    item = (raw.get('items') or [{}])[0]
    caption = (item.get('caption') or {}).get('text')
    author = (item.get('user') or {}).get('username')
    return _download(media_from_api_item(item), format_type, workdir, _shortcode(url), caption, author, route)


STRATEGIES = [
    Strategy("ig-embed", _embed),
    Strategy("ig-polaris", _polaris),
    Strategy("ytdlp-ig", _ytdlp),
    Strategy("cobalt-ig", _cobalt, route_sensitive=False, enabled=cobalt.enabled),
    Strategy("ig-opengraph", _opengraph),
    Strategy("ig-instagrapi", _instagrapi, requires_auth=True, enabled=_instagrapi_enabled),
]
