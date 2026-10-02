"""Network egress routes (direct IPv4, rotating IPv6, Cloudflare WARP, proxy).

Every strategy builds its HTTP session and yt-dlp options from a Route, so all
traffic of one attempt leaves through the same exit.
"""
import ipaddress
import logging
import random
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import config
from .errors import http_error

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    cffi_requests = None
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

IMPERSONATE = "chrome"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Route:
    name: str
    proxy: Optional[str] = None
    # IPv6 prefix to pick a fresh random source address from (per session)
    ipv6_prefix: Optional[str] = None

    def source_address(self) -> Optional[str]:
        if not self.ipv6_prefix:
            return None
        return random_ipv6(self.ipv6_prefix)

    def __str__(self) -> str:
        return self.name


DIRECT = Route("directo")


def random_ipv6(prefix: str) -> str:
    net = ipaddress.IPv6Network(prefix, strict=False)
    if net.num_addresses == 1:
        return str(net.network_address)
    # Skip ::0 and ::1, which are usually the VPS's own configured addresses
    offset = random.randint(2, net.num_addresses - 1)
    return str(net.network_address + offset)


def available_routes() -> List[Route]:
    """Routes in the order they should be tried."""
    routes = [DIRECT]
    if config.IPV6_PREFIX:
        routes.append(Route("ipv6", ipv6_prefix=config.IPV6_PREFIX))
    if config.WARP_PROXY:
        routes.append(Route("warp", proxy=config.WARP_PROXY))
    if config.PROXY:
        routes.append(Route("proxy", proxy=config.PROXY))
    return routes


class Http:
    """Thin curl_cffi session wrapper bound to one route, impersonating Chrome."""

    def __init__(self, route: Route = DIRECT, timeout: int = 20):
        if not HAS_CURL_CFFI:
            raise RuntimeError("curl_cffi no está instalado (pip install curl-cffi)")
        self.route = route
        self.timeout = timeout
        kwargs: Dict[str, Any] = {"impersonate": IMPERSONATE}
        if route.proxy:
            kwargs["proxy"] = route.proxy
        src = route.source_address()
        if src:
            kwargs["interface"] = src
        self.session = cffi_requests.Session(**kwargs)

    def get(self, url: str, check: bool = True, **kw):
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", True)
        resp = self.session.get(url, **kw)
        if check and resp.status_code >= 400:
            raise http_error(resp.status_code, f"GET {url.split('?')[0]} -> HTTP {resp.status_code}")
        return resp

    def post(self, url: str, check: bool = True, **kw):
        kw.setdefault("timeout", self.timeout)
        resp = self.session.post(url, **kw)
        if check and resp.status_code >= 400:
            raise http_error(resp.status_code, f"POST {url.split('?')[0]} -> HTTP {resp.status_code}")
        return resp

    def get_json(self, url: str, **kw) -> Any:
        return self.get(url, **kw).json()

    def download(self, url: str, dest: str, headers: Optional[Dict[str, str]] = None,
                 max_bytes: Optional[int] = None) -> str:
        """Streams url to dest. Raises on HTTP errors or empty bodies."""
        written = 0
        resp = self.session.get(url, headers=headers, timeout=120, stream=True, allow_redirects=True)
        try:
            if resp.status_code >= 400:
                raise http_error(resp.status_code, f"descarga de medio -> HTTP {resp.status_code}")
            with open(dest, "wb") as f:
                for chunk in resp.iter_content():
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise ValueError(f"El archivo supera {max_bytes // (1024 * 1024)} MB.")
                    f.write(chunk)
        finally:
            resp.close()
        if written == 0:
            raise ValueError("La descarga devolvió un archivo vacío.")
        return dest

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ydl_route_opts(route: Route) -> Dict[str, Any]:
    """yt-dlp options that send all traffic through the given route."""
    opts: Dict[str, Any] = {}
    if route.proxy:
        opts["proxy"] = route.proxy
    src = route.source_address()
    if src:
        opts["source_address"] = src
    return opts


def has_ipv6_connectivity(timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection(("2606:4700:4700::1111", 443), timeout=timeout):
            return True
    except OSError:
        return False
