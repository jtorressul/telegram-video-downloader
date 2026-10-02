"""Shared helpers: URL parsing, ffmpeg, thumbnails, ID3 tags and the generic yt-dlp engine."""
import glob
import logging
import os
import re
import subprocess
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import yt_dlp

import config
from .errors import TooLarge, Unsupported, classify
from .net import Http, Route, ydl_route_opts

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
    HAS_MUTAGEN = True
except ImportError:  # pragma: no cover
    HAS_MUTAGEN = False

logger = logging.getLogger(__name__)

MAX_TELEGRAM_SIZE_BYTES = config.MAX_TELEGRAM_SIZE_BYTES
TARGET_COMPRESSION_BYTES = int(MAX_TELEGRAM_SIZE_BYTES * 0.9)
YTDL_MAX_FILESIZE = 120 * 1024 * 1024


# ------------------------------------------------------------------------------
# URL helpers
# ------------------------------------------------------------------------------
def extract_youtube_id(url: str) -> Optional[str]:
    """Extracts 11-character YouTube video ID."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path
        if any(d in netloc for d in ['youtube.com', 'youtu.be']):
            if path in ['/watch', '/watch_popup']:
                v = urllib.parse.parse_qs(parsed.query).get('v')
                if v and len(v[0]) == 11:
                    return v[0]
            elif path.startswith(('/embed/', '/v/', '/shorts/', '/e/', '/live/')):
                parts = path.strip('/').split('/')
                if len(parts) >= 2 and len(parts[1]) == 11:
                    return parts[1]
            elif 'youtu.be' in netloc:
                parts = path.strip('/').split('/')
                if parts and len(parts[0]) == 11:
                    return parts[0]
    except Exception:
        pass
    m = re.search(r'(?:[?&]v=|youtu\.be/|/embed/|/shorts/|/live/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


def is_youtube_url(url: str) -> bool:
    return extract_youtube_id(url) is not None


def is_spotify_url(url: str) -> bool:
    url_lower = url.lower()
    return any(k in url_lower for k in ['spotify.com', 'spotify.link'])


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Desconocida"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def detect_platform(url: str) -> Tuple[str, str]:
    """Identifies the platform and corresponding emoji."""
    host = (urllib.parse.urlparse(url).netloc or url).lower()
    if any(k in host for k in ['spotify.com', 'spotify.link']):
        return "Spotify", "🟢"
    if 'tiktok.com' in host:
        return "TikTok", "🎵"
    if any(k in host for k in ['instagram.com', 'instagr.am', 'ig.me']):
        return "Instagram", "📸"
    if any(host == k or host.endswith('.' + k) for k in ['twitter.com', 'x.com', 'fxtwitter.com', 'vxtwitter.com', 'fixupx.com']):
        return "X (Twitter)", "🐦"
    if 'music.youtube.com' in host:
        return "YouTube Music", "🎵"
    if any(k in host for k in ['youtube.com', 'youtu.be']):
        return "YouTube", "▶️"
    if any(k in host for k in ['facebook.com', 'fb.watch', 'fb.com']):
        return "Facebook", "👥"
    if 'reddit.com' in host or 'redd.it' in host:
        return "Reddit", "🤖"
    if 'threads.net' in host or 'threads.com' in host:
        return "Threads", "🧵"
    return "Web Video", "🌐"


# ------------------------------------------------------------------------------
# Media helpers
# ------------------------------------------------------------------------------
def convert_thumbnail_to_jpg(thumb_path: Optional[str], video_path: str, output_dir: str) -> Optional[str]:
    """Converts thumbnail to JPG for Telegram compatibility (or grabs a frame from the video)."""
    if thumb_path and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        base, ext = os.path.splitext(thumb_path)
        if ext.lower() in ['.jpg', '.jpeg']:
            return thumb_path
        target_path = os.path.join(output_dir, f"thumb_{os.path.basename(base)}.jpg")
        if _ffmpeg(['-i', thumb_path, '-frames:v', '1', target_path], 15) and _nonempty(target_path):
            return target_path

    if video_path and _nonempty(video_path):
        target_path = os.path.join(output_dir, "frame_thumb.jpg")
        if _ffmpeg(['-ss', '00:00:01', '-i', video_path, '-vframes', '1', '-q:v', '2', target_path], 15) \
                and _nonempty(target_path):
            return target_path
    return None


def compress_video_ffmpeg(input_file: str, output_file: str, duration: float) -> bool:
    """Compresses video to fit within Telegram's upload limit."""
    if duration <= 0:
        return False
    target_total_bitrate = int(TARGET_COMPRESSION_BYTES * 8 / duration)
    video_bitrate = max(150 * 1000, target_total_bitrate - 96 * 1000)
    return _ffmpeg([
        '-i', input_file, '-c:v', 'libx264', '-b:v', str(video_bitrate),
        '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '96k', output_file,
    ], 240) and _nonempty(output_file)


def to_mp3(input_file: str, mp3_file: str) -> str:
    """Extracts audio as MP3. Raises Unsupported when the media has no audio track."""
    if _ffmpeg(['-i', input_file, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file], 120) \
            and _nonempty(mp3_file):
        return mp3_file
    raise Unsupported("ffmpeg no pudo extraer audio", user_message="🔇 Este video no tiene pista de audio.")


def gif_to_mp4(input_file: str, mp4_file: str) -> str:
    if _ffmpeg(['-i', input_file, '-movflags', 'faststart', '-pix_fmt', 'yuv420p',
                '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', mp4_file], 120) and _nonempty(mp4_file):
        return mp4_file
    return input_file


def apply_id3_tags(mp3_file: str, title: str, artist: str, cover_path: Optional[str] = None,
                   album: Optional[str] = None):
    """Embeds ID3 metadata and cover art into an MP3 file."""
    if not HAS_MUTAGEN or not os.path.exists(mp3_file) or not mp3_file.lower().endswith('.mp3'):
        return
    try:
        audio = ID3(mp3_file)
    except Exception:
        audio = ID3()
    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=artist))
    audio.add(TALB(encoding=3, text=album or title))
    if cover_path and _nonempty(cover_path):
        try:
            with open(cover_path, 'rb') as f:
                cover_data = f.read()
            mime = 'image/png' if cover_path.lower().endswith('.png') else 'image/jpeg'
            audio.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=cover_data))
        except Exception as e:
            logger.warning(f"No se pudo incrustar la carátula en ID3: {e}")
    try:
        audio.save(mp3_file, v2_version=3)
    except Exception as e:
        logger.warning(f"Error al guardar tags ID3: {e}")


def _ffmpeg(args: List[str], timeout: int) -> bool:
    try:
        res = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error'] + args,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return res.returncode == 0
    except Exception as e:
        logger.warning(f"ffmpeg falló: {e}")
        return False


def _nonempty(path: Optional[str]) -> bool:
    return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0


# ------------------------------------------------------------------------------
# Result builders
# ------------------------------------------------------------------------------
def media_result(kind: str, file_path: str, platform: str, emoji: str, title: str, artist: str,
                 thumbnail_path: Optional[str] = None, duration: Any = None,
                 width: Any = None, height: Any = None) -> Dict[str, Any]:
    is_audio = kind == 'audio'
    return {
        'type': kind,
        'file_path': file_path,
        'thumbnail_path': thumbnail_path,
        'title': title,
        'artist': artist,
        'duration': int(duration) if duration else None,
        'width': width,
        'height': height,
        'filesize': os.path.getsize(file_path),
        'platform': platform,
        'platform_emoji': emoji,
        'is_audio': is_audio,
        'format': 'mp3' if is_audio else ('jpg' if kind == 'photo' else 'mp4'),
    }


def carousel_result(items: List[Dict[str, Any]], platform: str, emoji: str, title: str,
                    artist: str) -> Dict[str, Any]:
    if not items:
        raise Unsupported("carrusel vacío")
    if len(items) == 1:
        single = dict(items[0])
        single.update(title=title, artist=artist, platform=platform, platform_emoji=emoji,
                      is_audio=single['type'] == 'audio',
                      format='jpg' if single['type'] == 'photo' else 'mp4')
        return single
    return {
        'type': 'carousel',
        'media_items': items,
        'title': title,
        'artist': artist,
        'duration': None,
        'filesize': sum(i['filesize'] for i in items),
        'platform': platform,
        'platform_emoji': emoji,
        'is_audio': False,
        'format': 'mp4',
    }


def download_media_list(http: Http, media: List[Dict[str, Any]], format_type: str, workdir: str,
                        prefix: str, platform: str, emoji: str, title: str, artist: str,
                        headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Downloads a normalized media list [{type: video|photo|gif, url, thumbnail_url, duration,
    width, height}] and builds the audio / single / carousel result."""
    if not media:
        raise Unsupported("La publicación no contiene videos ni imágenes descargables.")

    if format_type == 'mp3':
        vids = [m for m in media if m.get('type') in ('video', 'gif') and m.get('url')]
        if not vids:
            raise Unsupported("Esta publicación no contiene video para extraer audio.")
        v = vids[0]
        raw = http.download(v['url'], os.path.join(workdir, f"{prefix}_raw.mp4"), headers=headers,
                            max_bytes=YTDL_MAX_FILESIZE)
        final = to_mp3(raw, os.path.join(workdir, f"{prefix}.mp3"))
        thumb = _fetch_thumb(http, v.get('thumbnail_url'), final, workdir, f"{prefix}_thumb", headers)
        apply_id3_tags(final, title, artist, thumb)
        res = media_result('audio', final, platform, emoji, title, artist, thumb, v.get('duration'))
        ensure_size(res)
        return res

    items = []
    for idx, m in enumerate(media[:10]):
        url = m.get('url')
        if not url:
            continue
        kind = m.get('type')
        if kind in ('video', 'gif'):
            path = http.download(url, os.path.join(workdir, f"{prefix}_{idx}.mp4"), headers=headers,
                                 max_bytes=YTDL_MAX_FILESIZE)
            if kind == 'gif' and not path.endswith('.mp4'):
                path = gif_to_mp4(path, os.path.join(workdir, f"{prefix}_{idx}_gif.mp4"))
            thumb = _fetch_thumb(http, m.get('thumbnail_url'), path, workdir, f"{prefix}_{idx}_thumb", headers)
            items.append(media_result('video', path, platform, emoji, title, artist, thumb,
                                      m.get('duration'), m.get('width'), m.get('height')))
        else:
            path = http.download(url, os.path.join(workdir, f"{prefix}_{idx}.jpg"), headers=headers)
            items.append(media_result('photo', path, platform, emoji, title, artist, None,
                                      None, m.get('width'), m.get('height')))
    result = carousel_result(items, platform, emoji, title, artist)
    ensure_size(result)
    return result


def _fetch_thumb(http: Http, url: Optional[str], media_path: str, workdir: str, name: str,
                 headers: Optional[Dict[str, str]]) -> Optional[str]:
    raw = None
    if url:
        try:
            raw = http.download(url, os.path.join(workdir, f"{name}.img"), headers=headers)
        except Exception:
            raw = None
    return convert_thumbnail_to_jpg(raw, media_path, workdir)


def ensure_size(result: Dict[str, Any]) -> None:
    """Compresses an oversized single video; raises TooLarge if it still doesn't fit."""
    items = result.get('media_items') or [result]
    for item in items:
        path = item.get('file_path')
        if not path:
            continue
        size = os.path.getsize(path)
        if size > MAX_TELEGRAM_SIZE_BYTES and item.get('type') == 'video':
            duration = item.get('duration') or probe_duration(path)
            if duration and 0 < duration < 1800:
                out = os.path.splitext(path)[0] + '_compressed.mp4'
                if compress_video_ffmpeg(path, out, float(duration)):
                    item['file_path'] = out
                    size = os.path.getsize(out)
        item['filesize'] = size
        if size > MAX_TELEGRAM_SIZE_BYTES:
            raise TooLarge(
                f"{size} bytes",
                user_message=(f"📦 El archivo pesa {size / (1024 * 1024):.1f} MB y supera el límite de "
                              f"{MAX_TELEGRAM_SIZE_BYTES // (1024 * 1024)} MB de Telegram."),
            )
    if result.get('media_items'):
        result['filesize'] = sum(i['filesize'] for i in result['media_items'])
    else:
        result['filesize'] = items[0]['filesize']


def probe_duration(path: str) -> Optional[float]:
    try:
        res = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=nw=1:nk=1', path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, text=True)
        return float(res.stdout.strip())
    except Exception:
        return None


# ------------------------------------------------------------------------------
# Generic yt-dlp engine
# ------------------------------------------------------------------------------
def ydl_base_opts(format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        'outtmpl': os.path.join(workdir, '%(id)s.%(ext)s'),
        'writethumbnail': True,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'max_filesize': YTDL_MAX_FILESIZE,
        'concurrent_fragment_downloads': 8,
        'socket_timeout': 15,
        'http_chunk_size': 10485760,
        'retries': 2,
        'fragment_retries': 5,
        'extractor_retries': 1,
    }
    opts.update(ydl_route_opts(route))
    if format_type == 'mp3':
        opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
        opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3',
                                   'preferredquality': '192'}]
    else:
        opts['format'] = (
            'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/'
            'bestvideo[height<=720]+bestaudio/'
            'best[height<=720][ext=mp4]/best[height<=720]/best'
        )
        opts['merge_output_format'] = 'mp4'
    return opts


def ydl_download(url: str, format_type: str, workdir: str, route: Route, platform: str, emoji: str,
                 extra_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Downloads one media item with yt-dlp and builds the standard result dict."""
    opts = ydl_base_opts(format_type, workdir, route)
    if extra_opts:
        opts.update(extra_opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        raise classify(e)
    if not info:
        raise classify(ValueError("yt-dlp no devolvió información"))

    if info.get('entries'):
        entries = [e for e in info['entries'] if e]
        videos = [e for e in entries if e.get('vcodec') not in (None, 'none')
                  or e.get('ext') in ('mp4', 'mov', 'mkv', 'webm')]
        info = (videos or entries)[0]

    exts = ('.mp3',) if format_type == 'mp3' else ('.mp4', '.mkv', '.webm', '.mov', '.m4a')
    result_file = find_output(workdir, info.get('id'), exts)
    if not result_file:
        raise FileNotFoundError("No se encontró el archivo multimedia descargado.")

    title = info.get('title') or info.get('description') or 'Video'
    uploader = info.get('uploader') or info.get('channel') or info.get('artist') or platform
    raw_thumb = None
    for ext in ('.jpg', '.jpeg', '.webp', '.png', '.image'):
        cand = glob.glob(os.path.join(workdir, f"*{ext}"))
        if cand:
            raw_thumb = cand[0]
            break
    thumb = convert_thumbnail_to_jpg(raw_thumb, result_file, workdir)
    kind = 'audio' if format_type == 'mp3' else 'video'
    if kind == 'audio':
        apply_id3_tags(result_file, title, uploader, thumb)
    res = media_result(kind, result_file, platform, emoji, title, uploader, thumb,
                       info.get('duration'), info.get('width'), info.get('height'))
    ensure_size(res)
    return res


def find_output(workdir: str, media_id: Optional[str], exts: Tuple[str, ...]) -> Optional[str]:
    files = [f for f in glob.glob(os.path.join(workdir, '*'))
             if f.lower().endswith(exts) and _nonempty(f) and '.part' not in f]
    if media_id:
        preferred = [f for f in files if os.path.basename(f).startswith(str(media_id))]
        if preferred:
            files = preferred
    files.sort(key=os.path.getsize, reverse=True)
    return files[0] if files else None
