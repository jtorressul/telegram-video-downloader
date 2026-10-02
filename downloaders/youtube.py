"""YouTube / YouTube Music: yt-dlp (PO-token plugin + client rotation) → Cobalt. Anonymous by default."""
import importlib.util
import logging
from typing import Any, Dict

import config
from . import cobalt
from .base import Strategy
from .common import extract_youtube_id, ydl_download
from .errors import Unsupported
from .net import Route

logger = logging.getLogger(__name__)
PLATFORM, EMOJI = "YouTube", "▶️"

# With bgutil-ytdlp-pot-provider installed, yt-dlp fetches PO tokens from the local
# bgutil server automatically; that is what YouTube demands from datacenter IPs.
def _has_pot_provider() -> bool:
    try:
        return importlib.util.find_spec("yt_dlp_plugins.extractor.getpot_bgutil_http") is not None
    except ImportError:
        return False


HAS_POT_PROVIDER = _has_pot_provider()

# yt-dlp queries all listed clients in one call and merges whatever formats each returns
CLIENTS_PRIMARY = ['default', 'tv', 'web_safari', 'mweb']
CLIENTS_FALLBACK = ['android_vr', 'ios', 'tv_simply']


def canonical(url: str) -> str:
    vid = extract_youtube_id(url)
    if not vid:
        raise Unsupported("URL de YouTube no válida.")
    return f"https://www.youtube.com/watch?v={vid}"


def _ytdlp_with(clients):
    def run(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
        args: Dict[str, Any] = {'youtube': {'player_client': clients}}
        if config.BGUTIL_BASE_URL:
            args['youtubepot-bgutilhttp'] = {'base_url': [config.BGUTIL_BASE_URL]}
        extra: Dict[str, Any] = {'extractor_args': args}
        return ydl_download(canonical(url), format_type, workdir, route, PLATFORM, EMOJI, extra)
    return run


def _ytdlp_cookies(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    return ydl_download(canonical(url), format_type, workdir, route, PLATFORM, EMOJI,
                        {'cookiefile': config.resolve_cookies_file()})


def _cobalt(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    return cobalt.download(canonical(url), format_type, workdir, PLATFORM, EMOJI)


STRATEGIES = [
    Strategy("yt-clients", _ytdlp_with(CLIENTS_PRIMARY)),
    Strategy("yt-clients-fallback", _ytdlp_with(CLIENTS_FALLBACK)),
    Strategy("cobalt-yt", _cobalt, route_sensitive=False, enabled=cobalt.enabled),
    Strategy("yt-cookies", _ytdlp_cookies, requires_auth=True,
             enabled=lambda: bool(config.resolve_cookies_file())),
]
