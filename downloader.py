import os
import re
import glob
import shutil
import tempfile
import asyncio
import subprocess
from typing import Dict, Any, Optional, Tuple
import yt_dlp

# Telegram Bot API limits standard bots to 50MB for upload
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
TARGET_COMPRESSION_BYTES = 45 * 1024 * 1024  # 45 MB

YT_REGEX = re.compile(
    r'(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?v=|embed/|shorts/|v/|e/)|youtu\.be/)([a-zA-Z0-9_-]{11})'
)


def extract_youtube_id(url: str) -> Optional[str]:
    """Extracts 11-character YouTube video ID."""
    match = YT_REGEX.search(url)
    return match.group(1) if match else None


def is_youtube_url(url: str) -> bool:
    """Checks if a URL is from YouTube or YouTube Music."""
    return extract_youtube_id(url) is not None


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
    if any(k in url_lower for k in ['tiktok.com']):
        return "TikTok", "🎵"
    elif any(k in url_lower for k in ['instagram.com', 'instagr.am']):
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


class VideoDownloader:
    def __init__(self, temp_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        self.temp_dir = temp_dir or tempfile.gettempdir()
        self.cookies_file = cookies_file if (cookies_file and os.path.exists(cookies_file)) else None

    def _sync_download(self, url: str, format_type: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """
        Synchronous download execution using yt-dlp.
        Supports YouTube (MP3/MP4), TikTok, Instagram, X (Twitter), Facebook, etc.
        """
        format_type = format_type.lower().strip()
        if format_type not in ['mp3', 'mp4']:
            format_type = 'mp4'

        platform_name, platform_emoji = detect_platform(url)
        is_yt = is_youtube_url(url)

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
            'http_headers': {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            },
        }

        # Mobile client impersonation for YouTube to avoid bot checks
        if is_yt:
            ydl_opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['android', 'ios'],
                }
            }

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

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except Exception as e:
                err_msg = str(e)
                if 'Private video' in err_msg or 'This video is private' in err_msg:
                    raise ValueError("El video es privado o no está disponible.")
                elif 'Sign in to confirm you’re not a bot' in err_msg:
                    raise ValueError("La plataforma solicitó verificación de bot. Reintenta en unos instantes.")
                elif 'Video unavailable' in err_msg:
                    raise ValueError("El video no está disponible o fue eliminado.")
                raise ValueError(f"Error al descargar: {err_msg}")

            if not info:
                raise ValueError("No se pudo obtener información del enlace proporcionado.")

            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            title = info.get('title', 'Video')
            uploader = info.get('uploader') or info.get('channel') or platform_name
            duration = info.get('duration', 0) or 0
            width = info.get('width')
            height = info.get('height')

            base_filename = ydl.prepare_filename(info)
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
