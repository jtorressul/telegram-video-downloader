"""Strategy chain: try each anonymous strategy over each network route until one works."""
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import config
from . import health
from .errors import DownloadError, Unknown, classify
from .net import DIRECT, Route, available_routes

logger = logging.getLogger(__name__)

# fn(url, format_type, workdir, route) -> result dict
StrategyFn = Callable[[str, str, str, Route], Dict[str, Any]]


@dataclass
class Strategy:
    name: str
    fn: StrategyFn
    # Uses cookies / accounts: skipped when ANONYMOUS_ONLY=true
    requires_auth: bool = False
    # False for third-party APIs (fxtwitter, Cobalt) that fetch from their own servers:
    # changing our egress IP doesn't help, so they run once over the direct route
    route_sensitive: bool = True
    enabled: Callable[[], bool] = lambda: True


def run_chain(platform: str, strategies: List[Strategy], url: str, format_type: str,
              workdir: str, routes: Optional[List[Route]] = None) -> Dict[str, Any]:
    routes = routes or available_routes()
    deadline = time.time() + config.DOWNLOAD_DEADLINE_SECONDS
    errors: List[DownloadError] = []

    for strat in strategies:
        if strat.requires_auth and config.ANONYMOUS_ONLY:
            continue
        if not strat.enabled():
            continue
        for route in (routes if strat.route_sensitive else [DIRECT]):
            if time.time() > deadline:
                logger.warning(f"[{platform}] presupuesto de tiempo agotado")
                return _raise_best(errors)
            if health.is_open(strat.name, route.name):
                logger.info(f"[{platform}] {strat.name}@{route} en pausa (circuit breaker)")
                continue
            # Each attempt gets its own folder so partial files never leak into the next try
            attempt_dir = tempfile.mkdtemp(dir=workdir, prefix=f"{strat.name}_{route.name}_")
            try:
                if strat.route_sensitive:
                    health.throttle(platform)
                result = strat.fn(url, format_type, attempt_dir, route)
                health.record_success(strat.name, route.name)
                logger.info(f"✅ [{platform}] descargado con {strat.name}@{route}")
                result.setdefault("strategy", f"{strat.name}@{route}")
                return result
            except Exception as exc:
                err = classify(exc)
                errors.append(err)
                health.record_failure(strat.name, route.name, err.detail or str(exc),
                                      trips_breaker=not err.content_error)
                logger.warning(f"[{platform}] {strat.name}@{route} falló: "
                               f"{type(err).__name__}: {(err.detail or str(exc))[:300]}")
                if err.content_error:
                    # The media itself is the problem: other routes won't change that
                    break
    return _raise_best(errors)


def _raise_best(errors: List[DownloadError]):
    if not errors:
        raise Unknown("No hay estrategias disponibles para esta plataforma.")
    raise max(errors, key=lambda e: e.priority)


def new_workdir(base: str) -> str:
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base, prefix="tgbot_")
