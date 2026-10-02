"""Centralized environment configuration for the downloader bot."""
import os
import logging
import tempfile

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str) -> list:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = [int(i) for i in _list("ADMIN_IDS") if i.isdigit()]

# Modo anónimo: nunca usar cookies ni sesiones de las plataformas (por defecto activado)
ANONYMOUS_ONLY = _bool("ANONYMOUS_ONLY", True)

# --- Rutas de salida de red (todas sin login) ---
# Bloque IPv6 del VPS para rotar dirección de origen por descarga, ej: 2a01:4f8:1c1c:abcd::/64
IPV6_PREFIX = os.getenv("IPV6_PREFIX", "").strip()
# Proxy SOCKS de Cloudflare WARP en el propio VPS (warp-cli en modo proxy)
WARP_PROXY = os.getenv("WARP_PROXY", "").strip()
# Proxy genérico opcional (http:// o socks5://)
PROXY = (os.getenv("YTDL_PROXY") or os.getenv("HTTP_PROXY") or "").strip()

# --- Servicios externos ---
_cobalt = _list("COBALT_INSTANCES")
_cobalt_single = os.getenv("COBALT_API_URL", "").strip()
if _cobalt_single and _cobalt_single not in _cobalt:
    _cobalt.insert(0, _cobalt_single)
COBALT_INSTANCES = _cobalt
COBALT_API_KEY = os.getenv("COBALT_API_KEY", "").strip()

# Endpoints compatibles con la API de TikWM (se prueban en orden)
TIKTOK_APIS = _list("TIKTOK_APIS") or ["https://www.tikwm.com/api/", "https://tikwm.com/api/"]

# --- Límites ---
MAX_TELEGRAM_SIZE_BYTES = _int("MAX_UPLOAD_MB", 50) * 1024 * 1024
SPOTIFY_MAX_TRACKS = _int("SPOTIFY_MAX_TRACKS", 25)
# Presupuesto de tiempo total por descarga (segundos) antes de rendirse
DOWNLOAD_DEADLINE_SECONDS = _int("DOWNLOAD_DEADLINE_SECONDS", 180)
# Peticiones por minuto permitidas por plataforma (para no quemar la IP)
RATE_LIMITS = {
    "Instagram": _int("RATE_LIMIT_INSTAGRAM", 20),
    "TikTok": _int("RATE_LIMIT_TIKTOK", 40),
    "YouTube": _int("RATE_LIMIT_YOUTUBE", 30),
}

# --- Credenciales opcionales (solo se usan si ANONYMOUS_ONLY=false) ---
IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
INSTAGRAM_SESSIONID = os.getenv("INSTAGRAM_SESSIONID", "").strip()


def resolve_cookies_file() -> str:
    """Returns a usable cookies.txt path or '' (always '' in anonymous mode)."""
    if ANONYMOUS_ONLY:
        return ""
    path = os.getenv("COOKIES_FILE", "cookies.txt").strip()
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    raw = os.getenv("YOUTUBE_COOKIES") or os.getenv("COOKIES_CONTENT")
    if raw:
        env_path = os.path.join(tempfile.gettempdir(), "server_cookies.txt")
        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(raw.strip() + "\n")
            return env_path
        except OSError as e:
            logger.warning(f"Error escribiendo cookies desde variable de entorno: {e}")
    return ""
