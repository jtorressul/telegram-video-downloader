"""Network diagnostics: which platforms answer through which egress route."""
import time
from typing import List

from .net import Http, Route, available_routes, has_ipv6_connectivity

# (label, url, statuses that mean "reachable and not blocked")
CHECKS = [
    ("TikWM", "https://www.tikwm.com/api/?url=https://www.tiktok.com/@scout2015/video/6718335390845095173", (200,)),
    ("TikTok", "https://www.tiktok.com/@scout2015/video/6718335390845095173", (200,)),
    ("Instagram", "https://www.instagram.com/p/aye83DjauH/embed/captioned/", (200,)),
    ("YouTube", "https://www.youtube.com/watch?v=jNQXAC9IVRw", (200,)),
    ("fxtwitter", "https://api.fxtwitter.com/i/status/1585341984679469056", (200,)),
    ("Spotify", "https://open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT", (200,)),
    ("SoundCloud", "https://soundcloud.com/", (200,)),
]
_BLOCK_MARKERS = ("confirm you’re not a bot", "confirm you're not a bot", "unusual traffic")


def _egress(route: Route) -> str:
    try:
        with Http(route, timeout=10) as http:
            info = http.get_json("https://ipinfo.io/json")
        return f"{info.get('ip')} · {info.get('org', '?')} · {info.get('country', '?')}"
    except Exception as e:
        return f"sin salida ({type(e).__name__})"


def _check(route: Route, url: str, ok_statuses) -> str:
    start = time.time()
    try:
        with Http(route, timeout=15) as http:
            resp = http.get(url, check=False)
        ms = int((time.time() - start) * 1000)
        body = resp.text[:200000].lower()
        if resp.status_code in ok_statuses and not any(m in body for m in _BLOCK_MARKERS):
            return f"✅ {resp.status_code} ({ms} ms)"
        if any(m in body for m in _BLOCK_MARKERS):
            return f"🚧 bloqueo anti-bot ({resp.status_code})"
        return f"❌ HTTP {resp.status_code}"
    except Exception as e:
        return f"❌ {type(e).__name__}: {str(e)[:60]}"


def run_diagnostics() -> str:
    lines: List[str] = []
    lines.append(f"IPv6 disponible: {'sí' if has_ipv6_connectivity() else 'no'}")
    for route in available_routes():
        lines.append("")
        lines.append(f"── Ruta {route.name}: {_egress(route)}")
        for label, url, ok in CHECKS:
            lines.append(f"   {label:<11} {_check(route, url, ok)}")
    return "\n".join(lines)
