"""Circuit breaker, per-platform rate limiting and per-strategy stats."""
import random
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

import config

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 600

_lock = threading.Lock()
# (strategy, route) -> [consecutive_failures, open_until]
_breakers: Dict[Tuple[str, str], list] = defaultdict(lambda: [0, 0.0])
# (strategy, route) -> {"ok": n, "fail": n, "last_error": str}
_stats: Dict[Tuple[str, str], dict] = defaultdict(lambda: {"ok": 0, "fail": 0, "last_error": ""})
_platform_calls: Dict[str, Deque[float]] = defaultdict(deque)


def is_open(strategy: str, route: str) -> bool:
    with _lock:
        _, open_until = _breakers[(strategy, route)]
        return time.time() < open_until


def record_success(strategy: str, route: str) -> None:
    with _lock:
        _breakers[(strategy, route)] = [0, 0.0]
        _stats[(strategy, route)]["ok"] += 1


def record_failure(strategy: str, route: str, error: str, trips_breaker: bool) -> None:
    """Content errors (private, not found) don't trip the breaker; network/IP errors do."""
    with _lock:
        st = _stats[(strategy, route)]
        st["fail"] += 1
        st["last_error"] = error[:200]
        if not trips_breaker:
            return
        b = _breakers[(strategy, route)]
        b[0] += 1
        if b[0] >= FAILURE_THRESHOLD:
            b[1] = time.time() + COOLDOWN_SECONDS
            b[0] = 0


def throttle(platform: str) -> None:
    """Blocks until a request slot is free for the platform (sliding 60 s window + jitter)."""
    limit = config.RATE_LIMITS.get(platform.split(" ")[0])
    if not limit:
        return
    while True:
        with _lock:
            calls = _platform_calls[platform]
            now = time.time()
            while calls and now - calls[0] > 60:
                calls.popleft()
            if len(calls) < limit:
                calls.append(now)
                break
            wait = 60 - (now - calls[0])
        time.sleep(min(max(wait, 0.5), 10))
    time.sleep(random.uniform(0.2, 1.2))


def snapshot() -> Dict[str, dict]:
    with _lock:
        now = time.time()
        out = {}
        for key, st in sorted(_stats.items()):
            open_until = _breakers[key][1]
            out[f"{key[0]} @ {key[1]}"] = {
                **st,
                "paused_s": int(open_until - now) if open_until > now else 0,
            }
        return out
