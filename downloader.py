import os
import re
import json
import glob
import shutil
import tempfile
import asyncio
import subprocess
import urllib.request
from typing import Dict, Any, Optional, Tuple
import yt_dlp

# Telegram Bot API limits standard bots to 50MB for upload
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
TARGET_COMPRESSION_BYTES = 45 * 1024 * 1024  # 45 MB


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
    """
    Identifies the platform from URL.
    Returns (Platform Name, Emoji).
    """
    url_lower = url.lower()
    if any(k in url_lower for k in ['spotify.com', 'spotify.link']):
        return "Spotify", "🟢"
    elif any(k in url_lower for k in ['tiktok.com']):
        return "TikTok", "🎵"
    elif any(k in url_lower for k in ['instagram.com', 'instagr.am']):
        return "Instagram", "📸"
    elif any(k in url_lower for k in ['facebook.com', 'fb.watch', 'fb.com']):
        return "Facebook", "👥"
    elif any(k in url_lower for k in ['youtube.com', 'youtu.be']):
        return "YouTube", "▶️"
    elif any(k in url_lower for k in ['twitter.com', 'x.com']):
        return "X (Twitter)", "🐦"
    elif 'reddit.com' in url_lower:
        return "Reddit", "🤖"
    elif 'threads.net' in url_lower:
        return "Threads", "🧵"
    else:
        return "Web Video", "🌐"


def convert_thumbnail_to_jpg(thumb_path: Optional[str], video_path: str, output_dir: str) -> Optional[str]:
    """
    Ensures the thumbnail is a JPG format acceptable by Telegram send_video.
    Generates one from the video if thumb_path is missing or unusable.
    """
    target_thumb = os.path.join(output_dir, 'thumb_tg.jpg')

    # If yt-dlp extracted a thumbnail, convert it to JPEG if needed
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

    # Fallback: extract frame at second 1 from the video
    try:
        cmd = ['ffmpeg', '-y', '-ss', '00:00:01', '-i', video_path, '-vframes', '1', '-vf', 'scale=320:-1', '-q:v', '3', target_thumb]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if res.returncode == 0 and os.path.exists(target_thumb) and os.path.getsize(target_thumb) > 0:
            return target_thumb
    except Exception:
        pass

    return None


def compress_video_ffmpeg(input_file: str, output_file: str, duration: float) -> bool:
    """
    Compresses video to fit within Telegram's 50MB limit using two-pass or single-pass ffmpeg.
    """
    if duration <= 0:
        return False

    # Calculate target bitrate (bits per second) for ~44MB
    total_bitrate = int((TARGET_COMPRESSION_BYTES * 8) / duration)
    audio_bitrate = 128 * 1000  # 128 kbps
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


class VideoDownloader:
    def __init__(self, temp_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        self.temp_dir = temp_dir or tempfile.gettempdir()
        self.cookies_file = cookies_file if (cookies_file and os.path.exists(cookies_file)) else None

    def _sync_download_spotify(self, url: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """Downloads high quality audio matching a Spotify track."""
        meta = get_spotify_info(url)
        title = meta['title']
        artist = meta['artist']
        search_query = f"{artist} - {title} audio" if artist != 'Artista Desconocido' else f"{title} audio"

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
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

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch1:{search_query}", download=True)
            except Exception as e:
                raise ValueError(f"No se pudo descargar el audio para '{artist} - {title}': {e}")

            if not info or 'entries' not in info or not info['entries']:
                raise ValueError(f"No se encontró el audio para '{artist} - {title}'.")

            entry = info['entries'][0]
            base_filename = ydl.prepare_filename(entry)
            mp3_file = os.path.splitext(base_filename)[0] + '.mp3'

            if not os.path.exists(mp3_file):
                for f in os.listdir(temp_subfolder):
                    if f.lower().endswith('.mp3'):
                        mp3_file = os.path.join(temp_subfolder, f)
                        break

            if not os.path.exists(mp3_file):
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
            }

    def _sync_download(self, url: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """
        Synchronous download execution using yt-dlp.
        """
        if any(k in url.lower() for k in ['spotify.com', 'spotify.link']):
            return self._sync_download_spotify(url, output_template, temp_subfolder)
        ydl_opts: Dict[str, Any] = {
            'outtmpl': output_template,
            # Best mp4 or webm, prefer compatible codecs
            'format': (
                'bestvideo[ext=mp4][vcodec^=avc1][filesize<48M]+bestaudio[ext=m4a]/'
                'bestvideo[filesize<45M]+bestaudio[filesize<5M]/'
                'best[filesize<49M][ext=mp4]/'
                'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
            ),
            'merge_output_format': 'mp4',
            'writethumbnail': True,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            # Speed optimizations
            'concurrent_fragment_downloads': 8,
            'socket_timeout': 15,
            'http_chunk_size': 10485760,  # 10 MB chunks
            'retries': 3,
            'fragment_retries': 5,
            'http_headers': {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            },
        }

        if self.cookies_file:
            ydl_opts['cookiefile'] = self.cookies_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except Exception as e:
                err_msg = str(e)
                if 'Private video' in err_msg or 'This video is private' in err_msg:
                    raise ValueError("El video es privado o requiere inicio de sesión.")
                elif 'Sign in to confirm you’re not a bot' in err_msg:
                    raise ValueError("La plataforma solicitó verificación de bot o inicio de sesión.")
                elif 'Video unavailable' in err_msg:
                    raise ValueError("El video no está disponible o fue eliminado.")
                raise ValueError(f"Error al descargar: {err_msg}")

            if not info:
                raise ValueError("No se pudo obtener información del enlace proporcionado.")

            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            title = info.get('title', 'Video sin título')
            duration = info.get('duration', 0) or 0
            width = info.get('width')
            height = info.get('height')

            base_filename = ydl.prepare_filename(info)
            video_file = None

            # Look for created video file
            candidates = [
                os.path.splitext(base_filename)[0] + '.mp4',
                base_filename,
            ]
            for c in candidates:
                if os.path.exists(c) and os.path.getsize(c) > 0:
                    video_file = c
                    break

            if not video_file:
                prefix = os.path.splitext(base_filename)[0]
                matches = glob.glob(f"{glob.escape(prefix)}*")
                for m in matches:
                    if m.lower().endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts')) and os.path.getsize(m) > 0:
                        video_file = m
                        break

            if not video_file or not os.path.exists(video_file):
                raise FileNotFoundError("No se encontró el archivo de video luego de la descarga.")

            file_size = os.path.getsize(video_file)

            # If file size is larger than Telegram limit, attempt compression if duration is reasonable
            if file_size > MAX_TELEGRAM_SIZE_BYTES:
                if duration and 0 < duration < 900:  # Less than 15 minutes
                    compressed_path = os.path.join(temp_subfolder, 'compressed_video.mp4')
                    compressed_ok = compress_video_ffmpeg(video_file, compressed_path, float(duration))
                    if compressed_ok:
                        video_file = compressed_path
                        file_size = os.path.getsize(video_file)

            if file_size > MAX_TELEGRAM_SIZE_BYTES:
                raise ValueError(
                    f"El video pesa {file_size / (1024 * 1024):.1f} MB, "
                    f"superando el límite de 50 MB de los bots de Telegram."
                )

            # Check and prepare thumbnail
            raw_thumb = None
            thumb_prefix = os.path.splitext(base_filename)[0]
            for ext in ['.jpg', '.jpeg', '.webp', '.png']:
                t_candidate = thumb_prefix + ext
                if os.path.exists(t_candidate):
                    raw_thumb = t_candidate
                    break

            final_thumb = convert_thumbnail_to_jpg(raw_thumb, video_file, temp_subfolder)
            platform_name, platform_emoji = detect_platform(url)

            return {
                'file_path': video_file,
                'thumbnail_path': final_thumb,
                'title': title,
                'duration': int(duration) if duration else None,
                'width': width,
                'height': height,
                'filesize': file_size,
                'platform': platform_name,
                'platform_emoji': platform_emoji,
            }

    async def download(self, url: str) -> Dict[str, Any]:
        """
        Asynchronously downloads a video. Returns metadata and paths.
        Always call cleanup(result) when finished!
        """
        temp_subfolder = tempfile.mkdtemp(dir=self.temp_dir, prefix="tgbot_")
        output_template = os.path.join(temp_subfolder, '%(id)s.%(ext)s')

        try:
            result = await asyncio.to_thread(self._sync_download, url, output_template, temp_subfolder)
            result['temp_dir'] = temp_subfolder
            return result
        except Exception:
            shutil.rmtree(temp_subfolder, ignore_errors=True)
            raise

    @staticmethod
    def cleanup(download_result: Dict[str, Any]) -> None:
        """
        Cleans up temporary directory and files.
        """
        temp_dir = download_result.get('temp_dir')
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
