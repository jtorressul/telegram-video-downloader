"""Typed download errors with honest user-facing messages.

All subclass ValueError so bot.py's existing `except ValueError` shows the message.
"""
import re
from typing import Optional


class DownloadError(ValueError):
    user_message = "No se pudo descargar el contenido."
    # Content errors describe the media itself; retrying via another network route won't help
    content_error = False
    # Higher = more informative when choosing which error to show after a failed chain
    priority = 0

    def __init__(self, detail: str = "", user_message: Optional[str] = None):
        self.detail = detail
        if user_message:
            self.user_message = user_message
        super().__init__(self.user_message)


class Unknown(DownloadError):
    user_message = "No se pudo descargar el contenido. Inténtalo de nuevo en unos minutos."
    priority = 1


class IPBlocked(DownloadError):
    user_message = (
        "🚧 La plataforma está bloqueando temporalmente al servidor del bot.\n"
        "No es culpa del enlace: inténtalo de nuevo en unos minutos."
    )
    priority = 2


class RateLimited(DownloadError):
    user_message = "⏳ Demasiadas descargas seguidas en esta plataforma. Espera un par de minutos."
    priority = 3


class NotFound(DownloadError):
    user_message = "🔍 El contenido no existe o fue eliminado."
    content_error = True
    priority = 4


class Unavailable(DownloadError):
    """The platform answers but returns nothing to logged-out visitors (deleted, private or +18)."""
    user_message = (
        "🔍 Este contenido no está disponible públicamente: puede ser +18, privado "
        "o haber sido eliminado."
    )
    content_error = True
    priority = 4


class RegionOrAgeLocked(DownloadError):
    user_message = (
        "🔞 La plataforma no muestra este contenido sin iniciar sesión "
        "(restricción de edad o de región). El bot solo descarga contenido público."
    )
    priority = 5


class Private(DownloadError):
    user_message = "🔒 Este contenido es privado. El bot solo descarga contenido público."
    content_error = True
    priority = 6


class Unsupported(DownloadError):
    user_message = "Esta publicación no contiene medios descargables."
    content_error = True
    priority = 7


class TooLarge(DownloadError):
    content_error = True
    priority = 8


_RULES = [
    # TikTok status codes: 10231/10204/10216/10222 are logged-out / region / age gates
    (RegionOrAgeLocked, r"status code (10231|10204|10216|10222)|confirm your age|age[- ]restricted|"
                        r"inappropriate for some users|not available in your (country|region)|"
                        r"geo.?restrict|video not available in your"),
    (Private, r"private video|this account is private|is private|privado"),
    (NotFound, r"\b404\b|not found|has been removed|no longer available|does not exist|"
               r"video unavailable|this post isn.t available|deleted"),
    (RateLimited, r"\b429\b|too many requests|rate.?limit"),
    # yt-dlp's Instagram message for posts hidden from logged-out visitors
    (Unavailable, r"empty media response"),
    (IPBlocked, r"\b403\b|forbidden|not a bot|sign in to confirm|login required|log in|"
                r"requested content is not available|blocked|captcha|checkpoint|"
                r"unable to extract|timed? ?out|connection (reset|refused)"),
]


def classify(exc: BaseException) -> DownloadError:
    """Maps any exception (yt-dlp, HTTP, API) to a typed DownloadError, keeping the raw detail."""
    if isinstance(exc, DownloadError):
        return exc
    text = str(exc)
    low = text.lower()
    for cls, pattern in _RULES:
        if re.search(pattern, low):
            return cls(text)
    return Unknown(text)


def http_error(status: int, detail: str = "") -> DownloadError:
    if status == 404:
        return NotFound(detail or "HTTP 404")
    if status == 429:
        return RateLimited(detail or "HTTP 429")
    if status in (401, 403):
        return IPBlocked(detail or f"HTTP {status}")
    return Unknown(detail or f"HTTP {status}")
