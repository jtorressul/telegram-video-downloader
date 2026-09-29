import os
import re
import json
import glob
import shutil
import tempfile
import asyncio
import logging
import subprocess
import urllib.request
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional, Tuple
import yt_dlp

logger = logging.getLogger(__name__)

# Telegram Bot API limits standard bots to 50MB for upload
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
TARGET_COMPRESSION_BYTES = 45 * 1024 * 1024  # 45 MB


def extract_youtube_id(url: str) -> Optional[str]:
    """Extracts 11-character YouTube video ID supporting all formats and arbitrary query parameters."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path
        if any(d in netloc for d in ['youtube.com', 'youtu.be']):
            if path in ['/watch', '/watch_popup']:
                qs = parse_qs(parsed.query)
                v = qs.get('v')
                if v and len(v[0]) == 11:
                    return v[0]
            elif path.startswith(('/embed/', '/v/', '/shorts/', '/e/')):
                parts = path.strip('/').split('/')
                if len(parts) >= 2 and len(parts[1]) == 11:
                    return parts[1]
            elif 'youtu.be' in netloc:
                parts = path.strip('/').split('/')
                if parts and len(parts[0]) == 11:
                    return parts[0]
    except Exception:
        pass

    # Regex fallback
    m = re.search(r'(?:[?&]v=|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


def is_youtube_url(url: str) -> bool:
    """Checks if a URL is from YouTube or YouTube Music."""
    return extract_youtube_id(url) is not None


def is_spotify_url(url: str) -> bool:
    """Checks if a URL is from Spotify."""
    url_lower = url.lower()
    return any(k in url_lower for k in ['spotify.com', 'spotify.link'])


def format_duration(seconds: Optional[int]) -> str:
    """Formats duration in seconds to HH:MM:SS or MM:SS."""
    if not seconds:
        return "Desconocida"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def detect_platform(url: str) -> Tuple[str, str]:
    """Identifies the platform and corresponding emoji."""
    url_lower = url.lower()
    if any(k in url_lower for k in ['spotify.com', 'spotify.link']):
        return "Spotify", "🟢"
    elif any(k in url_lower for k in ['tiktok.com']):
        return "TikTok", "🎵"
    elif any(k in url_lower for k in ['instagram.com', 'instagr.am', 'ig.me']):
        return "Instagram", "📸"
    elif any(k in url_lower for k in ['twitter.com', 'x.com']):
        return "X (Twitter)", "🐦"
    elif 'music.youtube.com' in url_lower:
        return "YouTube Music", "🎵"
    elif any(k in url_lower for k in ['youtube.com', 'youtu.be']):
        return "YouTube", "▶️"
    elif any(k in url_lower for k in ['facebook.com', 'fb.watch', 'fb.com']):
        return "Facebook", "👥"
    elif 'reddit.com' in url_lower:
        return "Reddit", "🤖"
    elif 'threads.net' in url_lower:
        return "Threads", "🧵"
    else:
        return "Web Video", "🌐"


def get_spotify_info(url: str) -> Dict[str, str]:
    """Extracts track title, artist, and cover art from a Spotify URL."""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.geturl()
    except Exception:
        final_url = url

    title = ""
    thumbnail_url = ""
    artist = ""

    # 1. Try Spotify oEmbed
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={final_url}"
        req_oe = urllib.request.Request(oembed_url, headers=headers)
        with urllib.request.urlopen(req_oe, timeout=10) as oe_resp:
            oe_data = json.loads(oe_resp.read().decode('utf-8'))
            title = oe_data.get('title', '')
            thumbnail_url = oe_data.get('thumbnail_url', '')
    except Exception:
        pass

    # 2. Extract track id and fetch embed page for artist
    track_match = re.search(r'track/([a-zA-Z0-9]+)', final_url)
    if track_match:
        track_id = track_match.group(1)
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        try:
            req_embed = urllib.request.Request(embed_url, headers=headers)
            with urllib.request.urlopen(req_embed, timeout=10) as em_resp:
                html_data = em_resp.read().decode('utf-8')
                m = re.search(r'<script id=\"__NEXT_DATA__\"[^>]*>([^<]+)</script>', html_data)
                if m:
                    parsed = json.loads(m.group(1))
                    entity = parsed['props']['pageProps']['state']['data']['entity']
                    artists = entity.get('artists', [])
                    if artists:
                        artist = ', '.join([a.get('name') for a in artists if a.get('name')])
                    if not title:
                        title = entity.get('name', '')
        except Exception:
            pass

    return {
        'title': title or 'Canción de Spotify',
        'artist': artist or 'Artista Desconocido',
        'thumbnail_url': thumbnail_url
    }


def convert_thumbnail_to_jpg(thumb_path: Optional[str], video_path: str, output_dir: str) -> Optional[str]:
    """Ensures the thumbnail is a JPG format acceptable by Telegram."""
    target_thumb = os.path.join(output_dir, 'thumb_tg.jpg')

    if thumb_path and os.path.exists(thumb_path):
        if thumb_path.lower().endswith(('.jpg', '.jpeg')):
            return thumb_path
        try:
            cmd = ['ffmpeg', '-y', '-i', thumb_path, '-vf', 'scale=320:-1', '-q:v', '3', target_thumb]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            if res.returncode == 0 and os.path.exists(target_thumb) and os.path.getsize(target_thumb) > 0:
                return target_thumb
        except Exception:
            pass

    # Fallback from video
    if video_path and os.path.exists(video_path) and not video_path.lower().endswith('.mp3'):
        try:
            cmd = ['ffmpeg', '-y', '-ss', '00:00:01', '-i', video_path, '-vframes', '1', '-vf', 'scale=320:-1', '-q:v', '3', target_thumb]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            if res.returncode == 0 and os.path.exists(target_thumb) and os.path.getsize(target_thumb) > 0:
                return target_thumb
        except Exception:
            pass

    return None


def compress_video_ffmpeg(input_file: str, output_file: str, duration: float) -> bool:
    """Compresses video to fit within Telegram's 50MB limit."""
    if duration <= 0:
        return False

    total_bitrate = int((TARGET_COMPRESSION_BYTES * 8) / duration)
    audio_bitrate = 128 * 1000
    video_bitrate = max(total_bitrate - audio_bitrate, 150 * 1000)

    try:
        cmd = [
            'ffmpeg', '-y', '-i', input_file,
            '-c:v', 'libx264',
            '-b:v', str(video_bitrate),
            '-maxrate', str(int(video_bitrate * 1.3)),
            '-bufsize', str(int(video_bitrate * 2)),
            '-preset', 'veryfast',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            output_file
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if res.returncode == 0 and os.path.exists(output_file):
            if os.path.getsize(output_file) <= MAX_TELEGRAM_SIZE_BYTES:
                return True
    except Exception:
        pass

    return False


def normalize_instagram_url(url: str) -> str:
    """
    Normalizes Instagram URLs to clean, standard formats recognized by extractors.
    Handles /share/reel/ID, /share/p/ID, /share/ID, query parameter stripping, etc.
    """
    if not url:
        return url

    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip('/')

        # 1. Convert share URLs: /share/reel/XYZ -> /reel/XYZ/ or /share/p/XYZ -> /p/XYZ/
        share_match = re.search(r'/share/(?:reel|p)/([a-zA-Z0-9_-]+)', path)
        if share_match:
            shortcode = share_match.group(1)
            path = f"/reel/{shortcode}"
        elif path.startswith('/share/'):
            # Generic /share/ID or shortlink redirect
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    redirected = resp.geturl()
                    if redirected and redirected != url and 'instagram.com' in redirected:
                        return normalize_instagram_url(redirected)
            except Exception:
                pass

        # 2. Convert /reels/ to /reel/
        if path.startswith('/reels/'):
            path = '/reel/' + path[7:]

        # 3. Strip tracking parameters (?igsh=..., utm_*, etc.)
        qs = parse_qs(parsed.query)
        filtered_qs = {
            k: v for k, v in qs.items()
            if not (k.startswith('utm_') or k in ['igsh', 'ig_mid', 'src', 'fbclid', 'ig_rid'])
        }
        from urllib.parse import urlencode
        clean_query = urlencode(filtered_qs, doseq=True) if filtered_qs else ""

        netloc = 'www.instagram.com' if any(d in parsed.netloc.lower() for d in ['instagr.am', 'instagram.com', 'ig.me']) else parsed.netloc
        normalized = f"{parsed.scheme or 'https'}://{netloc}{path}/"
        if clean_query:
            normalized += f"?{clean_query}"
        return normalized
    except Exception:
        return url


def setup_cookies_file(cookies_path: str = "cookies.txt") -> Optional[str]:
    """
    Sets up cookies from environment variable if present, or checks existing file.
    Supports YOUTUBE_COOKIES, COOKIES_CONTENT, INSTAGRAM_COOKIES, and INSTAGRAM_SESSIONID.
    """
    cookies_env = (
        os.getenv("COOKIES_CONTENT")
        or os.getenv("YOUTUBE_COOKIES")
        or os.getenv("INSTAGRAM_COOKIES")
    )
    ig_sessionid = os.getenv("INSTAGRAM_SESSIONID") or os.getenv("IG_SESSIONID")

    lines = []
    if cookies_env and cookies_env.strip():
        lines.append(cookies_env.strip())

    if ig_sessionid and ig_sessionid.strip():
        sid = ig_sessionid.strip()
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{sid}")

    if lines:
        try:
            with open(cookies_path, "w", encoding="utf-8") as f:
                header = "# Netscape HTTP Cookie File\n" if not any(l.startswith("#") for l in lines) else ""
                f.write(header + "\n".join(lines) + "\n")
            return cookies_path
        except Exception as e:
            logger.warning(f"Error escribiendo archivo de cookies: {e}")

    if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
        return cookies_path
    return None


def get_js_runtimes_config() -> Optional[Dict[str, Any]]:
    """Detects available JS runtime (node/deno) for yt-dlp challenge solving."""
    node_path = shutil.which("node") or shutil.which("nodejs")
    if node_path:
        return {'node': {'path': node_path}}
    deno_path = shutil.which("deno")
    if deno_path:
        return {'deno': {'path': deno_path}}
    return None


class VideoDownloader:
    def __init__(self, temp_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        self.temp_dir = temp_dir or tempfile.gettempdir()
        self.cookies_file = setup_cookies_file(cookies_file or "cookies.txt")

    def _sync_download_spotify(self, url: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """Downloads high quality audio matching a Spotify track using SoundCloud (primary) or YouTube (fallback)."""
        meta = get_spotify_info(url)
        title = meta['title']
        artist = meta['artist']
        search_query = f"{artist} {title}" if artist and artist != 'Artista Desconocido' else title

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'max_filesize': MAX_TELEGRAM_SIZE_BYTES,
        }

        if self.cookies_file:
            ydl_opts['cookiefile'] = self.cookies_file

        info = None
        ydl_used = None

        # 1. Try SoundCloud first (reliable, 0 IP blocks on cloud datacenter IPs)
        try:
            ydl_sc = yt_dlp.YoutubeDL(ydl_opts)
            sc_info = ydl_sc.extract_info(f"scsearch1:{search_query}", download=True)
            if sc_info and 'entries' in sc_info and sc_info['entries']:
                info = sc_info
                ydl_used = ydl_sc
        except Exception as e:
            logger.info(f"SoundCloud search for '{search_query}' failed: {e}")

        # 2. Fallback to YouTube if SoundCloud didn't find the track
        if not info or not info.get('entries'):
            try:
                yt_query = f"{artist} - {title} audio" if artist else f"{title} audio"
                yt_opts = dict(ydl_opts)
                js_cfg = get_js_runtimes_config()
                if js_cfg:
                    yt_opts['js_runtimes'] = js_cfg
                yt_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
                ydl_yt = yt_dlp.YoutubeDL(yt_opts)
                yt_info = ydl_yt.extract_info(f"ytsearch1:{yt_query}", download=True)
                if yt_info and 'entries' in yt_info and yt_info['entries']:
                    info = yt_info
                    ydl_used = ydl_yt
            except Exception as e2:
                logger.warning(f"YouTube fallback for '{search_query}' failed: {e2}")

        if not info or not info.get('entries'):
            raise ValueError(f"No se encontró el audio para '{artist} - {title}'.")

        entry = info['entries'][0]
        base_filename = ydl_used.prepare_filename(entry) if ydl_used else ""
        mp3_file = os.path.splitext(base_filename)[0] + '.mp3' if base_filename else None

        if not mp3_file or not os.path.exists(mp3_file):
            for f in os.listdir(temp_subfolder):
                if f.lower().endswith('.mp3'):
                    mp3_file = os.path.join(temp_subfolder, f)
                    break

        if not mp3_file or not os.path.exists(mp3_file):
            raise FileNotFoundError("No se encontró el archivo MP3 descargado.")

        file_size = os.path.getsize(mp3_file)
        duration = entry.get('duration') or 0

        # Download album cover
        thumb_file = None
        if meta.get('thumbnail_url'):
            thumb_file = os.path.join(temp_subfolder, 'spotify_cover.jpg')
            try:
                urllib.request.urlretrieve(meta['thumbnail_url'], thumb_file)
            except Exception:
                thumb_file = None

        return {
            'file_path': mp3_file,
            'thumbnail_path': thumb_file,
            'title': title,
            'artist': artist,
            'duration': int(duration) if duration else None,
            'width': None,
            'height': None,
            'filesize': file_size,
            'platform': 'Spotify',
            'platform_emoji': '🟢',
            'is_audio': True,
            'format': 'mp3',
        }

    def _sync_download(self, url: str, format_type: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """
        Synchronous download execution using yt-dlp.
        Supports YouTube (MP3/MP4), TikTok, Instagram, X (Twitter), Facebook, etc.
        """
        if is_spotify_url(url):
            return self._sync_download_spotify(url, output_template, temp_subfolder)

        format_type = format_type.lower().strip()
        if format_type not in ['mp3', 'mp4']:
            format_type = 'mp4'

        platform_name, platform_emoji = detect_platform(url)
        is_yt = is_youtube_url(url)

        if platform_name == "Instagram":
            url = normalize_instagram_url(url)

        ydl_opts: Dict[str, Any] = {
            'outtmpl': output_template,
            'writethumbnail': True,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'max_filesize': MAX_TELEGRAM_SIZE_BYTES,
            'concurrent_fragment_downloads': 8,
            'socket_timeout': 15,
            'http_chunk_size': 10485760,
            'retries': 3,
            'fragment_retries': 5,
        }

        # For non-YouTube platforms, set standard User-Agent.
        # For YouTube, DO NOT override User-Agent so yt-dlp matches internal client signatures.
        if not is_yt:
            ydl_opts['http_headers'] = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            }

        # JS runtime configuration for n-challenge solving
        js_cfg = get_js_runtimes_config()
        if js_cfg:
            ydl_opts['js_runtimes'] = js_cfg

        if format_type == 'mp3':
            ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            ydl_opts['format'] = (
                'bestvideo[ext=mp4][vcodec^=avc1][filesize<48M]+bestaudio[ext=m4a]/'
                'bestvideo[filesize<45M]+bestaudio[filesize<5M]/'
                'best[filesize<49M][ext=mp4]/'
                'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
            )
            ydl_opts['merge_output_format'] = 'mp4'

        if self.cookies_file:
            ydl_opts['cookiefile'] = self.cookies_file

        # Execution with sequential client fallbacks for YouTube
        clients_to_try = [['android'], None, ['web_safari'], ['mweb']] if is_yt else [None]
        info = None
        active_ydl = None
        last_error = None

        for client in clients_to_try:
            attempt_opts = dict(ydl_opts)
            if client:
                attempt_opts['extractor_args'] = {'youtube': {'player_client': client}}
            try:
                active_ydl = yt_dlp.YoutubeDL(attempt_opts)
                info = active_ydl.extract_info(url, download=True)
                if info:
                    break
            except Exception as e:
                last_error = e
                err_msg = str(e)
                logger.warning(f"Download attempt with client {client} failed: {err_msg}")
                if 'Private video' in err_msg or 'This video is private' in err_msg:
                    raise ValueError("El video es privado o no está disponible.")
                elif 'Video unavailable' in err_msg:
                    raise ValueError("El video no está disponible o fue eliminado.")
                continue

        if not info:
            err_text = str(last_error or "Error desconocido")
            if 'Sign in to confirm you’re not a bot' in err_text or 'bot' in err_text.lower() or 'Failed to extract any player response' in err_text:
                raise ValueError(
                    "YouTube ha bloqueado temporalmente las descargas para la IP del servidor en la nube (Render). "
                    "Para solucionarlo de inmediato, añade tus cookies en la variable de entorno YOUTUBE_COOKIES en Render."
                )
            if platform_name == "Instagram":
                err_lower = err_text.lower()
                if any(k in err_lower for k in [
                    'ciertas audiencias', 'audiences', 'restricted', 'restricción',
                    'empty media response', 'login', 'checkpoint', 'disponible para todo el mundo'
                ]):
                    raise ValueError(
                        "🔒 Video con Restricción de Edad o Audiencia en Instagram.\n\n"
                        "Instagram bloquea el acceso anónimo a este contenido (+18 o audiencia sensible).\n\n"
                        "💡 Para descargar videos restringidos: Añade tu sesión de Instagram en tu archivo .env:\n"
                        "INSTAGRAM_SESSIONID=tu_session_id\n\n"
                        "(Obtén el valor de la cookie 'sessionid' desde instagram.com en tu navegador -> F12 -> Almacenamiento/Storage -> Cookies)"
                    )
            raise ValueError(f"Error al descargar: {err_text}")

        if 'entries' in info and info['entries']:
            # Prioritize video entries in carousel posts
            video_entries = [
                e for e in info['entries']
                if e and (
                    e.get('vcodec') not in (None, 'none')
                    or e.get('ext') in ['mp4', 'mov', 'mkv', 'webm']
                    or e.get('video_ext') not in (None, 'none')
                )
            ]
            if video_entries:
                info = video_entries[0]
            else:
                info = info['entries'][0]

        title = info.get('title', 'Video')
        uploader = info.get('uploader') or info.get('channel') or platform_name
        duration = info.get('duration', 0) or 0
        width = info.get('width')
        height = info.get('height')

        base_filename = active_ydl.prepare_filename(info)
        result_file = None

        if format_type == 'mp3':
            target_mp3 = os.path.splitext(base_filename)[0] + '.mp3'
            if os.path.exists(target_mp3):
                result_file = target_mp3
            else:
                for f in os.listdir(temp_subfolder):
                    if f.lower().endswith('.mp3'):
                        result_file = os.path.join(temp_subfolder, f)
                        break
        else:
            candidates = [
                os.path.splitext(base_filename)[0] + '.mp4',
                base_filename,
            ]
            for c in candidates:
                if os.path.exists(c) and os.path.getsize(c) > 0:
                    result_file = c
                    break

            if not result_file:
                prefix = os.path.splitext(base_filename)[0]
                matches = glob.glob(f"{glob.escape(prefix)}*")
                for m in matches:
                    if m.lower().endswith(('.mp4', '.mkv', '.webm', '.mov')) and os.path.getsize(m) > 0:
                        result_file = m
                        break

        if not result_file or not os.path.exists(result_file):
            raise FileNotFoundError(f"No se encontró el archivo descargado.")

        file_size = os.path.getsize(result_file)

        # Compress video if slightly over 50MB
        if format_type == 'mp4' and file_size > MAX_TELEGRAM_SIZE_BYTES:
            if duration and 0 < duration < 900:
                compressed_path = os.path.join(temp_subfolder, 'compressed_video.mp4')
                if compress_video_ffmpeg(result_file, compressed_path, float(duration)):
                    result_file = compressed_path
                    file_size = os.path.getsize(result_file)

        if file_size > MAX_TELEGRAM_SIZE_BYTES:
            raise ValueError(
                f"El archivo pesa {file_size / (1024 * 1024):.1f} MB, "
                f"superando el límite de 50 MB de Telegram."
            )

        # Look for thumbnail
        raw_thumb = None
        thumb_prefix = os.path.splitext(base_filename)[0]
        for ext in ['.jpg', '.jpeg', '.webp', '.png']:
            t_cand = thumb_prefix + ext
            if os.path.exists(t_cand):
                raw_thumb = t_cand
                break

        final_thumb = convert_thumbnail_to_jpg(raw_thumb, result_file, temp_subfolder)

        return {
            'file_path': result_file,
            'thumbnail_path': final_thumb,
            'title': title,
            'artist': uploader,
            'duration': int(duration) if duration else None,
            'width': width,
            'height': height,
            'filesize': file_size,
            'platform': platform_name,
            'platform_emoji': platform_emoji,
            'is_audio': (format_type == 'mp3'),
            'format': format_type,
        }

    async def download(self, url: str, format_type: str = "mp4") -> Dict[str, Any]:
        """Asynchronously downloads media."""
        temp_subfolder = tempfile.mkdtemp(dir=self.temp_dir, prefix="tgbot_")
        output_template = os.path.join(temp_subfolder, '%(id)s.%(ext)s')

        try:
            result = await asyncio.to_thread(
                self._sync_download, url, format_type, output_template, temp_subfolder
            )
            result['temp_dir'] = temp_subfolder
            return result
        except Exception:
            shutil.rmtree(temp_subfolder, ignore_errors=True)
            raise

    @staticmethod
    def cleanup(download_result: Dict[str, Any]) -> None:
        """Cleans up temporary directory and files."""
        temp_dir = download_result.get('temp_dir')
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
