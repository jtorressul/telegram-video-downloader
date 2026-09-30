import os
import re
import json
import html
import glob
import shutil
import tempfile
import asyncio
import logging
import subprocess
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional, Tuple, List

import yt_dlp

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False

try:
    import mutagen
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

logger = logging.getLogger(__name__)

# Telegram Bot API limit: 50MB
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024
TARGET_COMPRESSION_BYTES = 45 * 1024 * 1024

# Public/Custom Cobalt instances pool for bridge downloads (configurable via COBALT_API_URL)
_custom_cobalt = os.getenv("COBALT_API_URL", "").strip()
COBALT_INSTANCES = [_custom_cobalt] if _custom_cobalt else []

# Instagram phantom account credentials (optional for full login-gated content access)
IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
INSTAGRAM_SESSIONID = os.getenv("INSTAGRAM_SESSIONID", "").strip()


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
                qs = urllib.parse.parse_qs(parsed.query)
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


def normalize_instagram_url(url: str) -> str:
    """Extracts clean canonical Instagram post or reel URL."""
    m = re.search(r'(?:instagram\.com|instagr\.am|ig\.me)/(p|reel|reels|tv|share/reel|share/p)/([a-zA-Z0-9_-]+)', url)
    if m:
        ptype = m.group(1).lower()
        if 'p' in ptype:
            return f"https://www.instagram.com/p/{m.group(2)}/"
        return f"https://www.instagram.com/reel/{m.group(2)}/"
    return url


def extract_instagram_shortcode(url: str) -> Optional[str]:
    """Extracts shortcode from Instagram URL."""
    m = re.search(r'(?:instagram\.com|instagr\.am|ig\.me)/(?:p|reel|reels|tv|share/reel|share/p)/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else None


def get_spotify_info(url: str) -> Dict[str, str]:
    """Extracts track title, artist, and cover art from a Spotify URL using oEmbed."""
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

    try:
        oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(final_url)}"
        req_oe = urllib.request.Request(oembed_url, headers=headers)
        with urllib.request.urlopen(req_oe, timeout=10) as oe_resp:
            oe_data = json.loads(oe_resp.read().decode('utf-8'))
            raw_title = oe_data.get('title', '')
            thumbnail_url = oe_data.get('thumbnail_url', '')

            # Parse "Title by Artist" or "Artist - Title"
            if ' by ' in raw_title:
                parts = raw_title.split(' by ')
                title = parts[0].strip()
                artist = parts[1].strip()
            elif ' - ' in raw_title:
                parts = raw_title.split(' - ')
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                title = raw_title
    except Exception as e:
        logger.warning(f"Error reading Spotify oEmbed: {e}")

    return {
        'title': title or "Spotify Track",
        'artist': artist or "Artista",
        'thumbnail_url': thumbnail_url or ""
    }


def convert_thumbnail_to_jpg(thumb_path: Optional[str], video_path: str, output_dir: str) -> Optional[str]:
    """Converts thumbnail to JPG for Telegram compatibility."""
    if thumb_path and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        base, ext = os.path.splitext(thumb_path)
        if ext.lower() in ['.jpg', '.jpeg']:
            return thumb_path
        target_path = os.path.join(output_dir, f"thumb_{os.path.basename(base)}.jpg")
        cmd = ['ffmpeg', '-y', '-i', thumb_path, '-frames:v', '1', target_path]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if res.returncode == 0 and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                return target_path
        except Exception:
            pass

    # Extract frame from video if available
    if video_path and os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        target_path = os.path.join(output_dir, "frame_thumb.jpg")
        cmd = ['ffmpeg', '-y', '-ss', '00:00:01', '-i', video_path, '-vframes', '1', '-q:v', '2', target_path]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if res.returncode == 0 and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                return target_path
        except Exception:
            pass

    return None


def compress_video_ffmpeg(input_file: str, output_file: str, duration: float) -> bool:
    """Compresses video to fit within Telegram's 50MB limit."""
    if duration <= 0:
        return False
    target_bits = TARGET_COMPRESSION_BYTES * 8
    target_total_bitrate = int(target_bits / duration)
    audio_bitrate = 96 * 1000
    video_bitrate = max(150 * 1000, target_total_bitrate - audio_bitrate)

    cmd = [
        'ffmpeg', '-y', '-i', input_file,
        '-c:v', 'libx264', '-b:v', str(video_bitrate),
        '-preset', 'veryfast',
        '-c:a', 'aac', '-b:a', '96k',
        output_file
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240)
        return res.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 0
    except Exception as e:
        logger.error(f"Error compressing video with ffmpeg: {e}")
        return False


def apply_id3_tags(mp3_file: str, title: str, artist: str, cover_path: Optional[str] = None):
    """Embeds ID3 metadata and official album cover into MP3 file using mutagen."""
    if not HAS_MUTAGEN or not os.path.exists(mp3_file):
        return
    try:
        audio = ID3(mp3_file)
    except Exception:
        audio = ID3()

    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=artist))
    audio.add(TALB(encoding=3, text=title))

    if cover_path and os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
        try:
            with open(cover_path, 'rb') as f:
                cover_data = f.read()
            mime = 'image/png' if cover_path.lower().endswith('.png') else 'image/jpeg'
            audio.add(APIC(
                encoding=3,
                mime=mime,
                type=3,  # Front cover
                desc='Cover',
                data=cover_data
            ))
        except Exception as e_cover:
            logger.warning(f"No se pudo incrustar la carátula en ID3: {e_cover}")

    try:
        audio.save(mp3_file, v2_version=3)
        logger.info(f"✅ Tags ID3 y carátula incrustados exitosamente en: {mp3_file}")
    except Exception as e:
        logger.warning(f"Error al guardar tags ID3 con mutagen: {e}")


# ==============================================================================
# 1. TIKTOK FAST EXTRACTOR (100% Zero-Cookies via TikWM)
# ==============================================================================
def download_tiktok_fast(url: str, format_type: str, temp_subfolder: str) -> Dict[str, Any]:
    """Downloads TikTok videos without watermark or photo slideshows directly via TikWM API."""
    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )
    }

    data = None
    last_err = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data and data.get('code') == 0:
                    break
        except Exception as e:
            last_err = e

    if not data or data.get('code') != 0:
        msg = data.get('msg') if data else str(last_err or "Error de conexión con TikWM")
        raise ValueError(f"No se pudo descargar el video de TikTok: {msg}")

    item = data.get('data') or {}
    title = item.get('title') or "Video de TikTok"
    author_obj = item.get('author') or {}
    author = author_obj.get('nickname') or author_obj.get('unique_id') or "TikTok"
    duration = item.get('duration') or 0
    cover_url = item.get('cover')

    # Audio MP3 format
    if format_type == 'mp3':
        music_url = item.get('music') or item.get('play')
        if not music_url:
            raise ValueError("No se encontró el audio de este TikTok.")
        raw_audio = os.path.join(temp_subfolder, "tiktok_audio_raw")
        urllib.request.urlretrieve(music_url, raw_audio)
        mp3_file = os.path.join(temp_subfolder, f"{item.get('id', 'tiktok')}.mp3")
        cmd = ['ffmpeg', '-y', '-i', raw_audio, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else raw_audio

        thumb_file = None
        if cover_url:
            raw_cov = os.path.join(temp_subfolder, "tiktok_cover.jpg")
            try:
                urllib.request.urlretrieve(cover_url, raw_cov)
                thumb_file = convert_thumbnail_to_jpg(raw_cov, "", temp_subfolder)
                apply_id3_tags(final_file, title, author, thumb_file)
            except Exception:
                pass

        return {
            'type': 'audio',
            'file_path': final_file,
            'thumbnail_path': thumb_file,
            'title': title,
            'artist': author,
            'duration': int(duration) if duration else None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(final_file),
            'platform': 'TikTok',
            'platform_emoji': '🎵',
            'is_audio': True,
            'format': 'mp3',
        }

    # TikTok Photo Slideshow
    images = item.get('images')
    if images and isinstance(images, list) and len(images) > 0:
        media_items = []
        for idx, img_url in enumerate(images[:10]):
            img_path = os.path.join(temp_subfolder, f"tiktok_img_{idx}.jpg")
            try:
                urllib.request.urlretrieve(img_url, img_path)
                media_items.append({'type': 'photo', 'file_path': img_path, 'filesize': os.path.getsize(img_path)})
            except Exception:
                pass
        if media_items:
            return {
                'type': 'carousel',
                'media_items': media_items,
                'title': title,
                'artist': author,
                'duration': None,
                'filesize': sum(m['filesize'] for m in media_items),
                'platform': 'TikTok',
                'platform_emoji': '🎵',
                'is_audio': False,
                'format': 'mp4',
            }

    # TikTok Video (Watermark-free)
    video_url = item.get('hdplay') or item.get('play')
    if not video_url:
        raise ValueError("No se pudo obtener el video de TikTok.")

    out_video = os.path.join(temp_subfolder, f"tiktok_{item.get('id', 'video')}.mp4")
    urllib.request.urlretrieve(video_url, out_video)

    thumb_file = None
    if cover_url:
        raw_cov = os.path.join(temp_subfolder, "tiktok_cover.jpg")
        try:
            urllib.request.urlretrieve(cover_url, raw_cov)
            thumb_file = convert_thumbnail_to_jpg(raw_cov, out_video, temp_subfolder)
        except Exception:
            pass

    return {
        'type': 'video',
        'file_path': out_video,
        'thumbnail_path': thumb_file,
        'title': title,
        'artist': author,
        'duration': int(duration) if duration else None,
        'width': None,
        'height': None,
        'filesize': os.path.getsize(out_video),
        'platform': 'TikTok',
        'platform_emoji': '🎵',
        'is_audio': False,
        'format': 'mp4',
    }


# ==============================================================================
# 2. X.COM (TWITTER) FAST EXTRACTOR (100% Zero-Cookies via FxTwitter)
# ==============================================================================
def download_twitter_fast(url: str, temp_subfolder: str, format_type: str = "mp4") -> Dict[str, Any]:
    """Downloads Twitter/X media using FxTwitter or VxTwitter API directly (bypasses 18+/NSFW auth and cloud IP blocks)."""
    m = re.search(r'(?:twitter\.com|x\.com)/(?:([a-zA-Z0-9_]+)/status/|i/status/)(\d+)', url)
    if not m:
        raise ValueError("URL de X (Twitter) no válida.")
    user = m.group(1) or "i"
    twid = m.group(2)

    data = None
    last_err = None
    endpoints = [
        f"https://api.fxtwitter.com/status/{twid}",
        f"https://api.fxtwitter.com/{user}/status/{twid}",
        f"https://api.vxtwitter.com/status/{twid}",
        f"https://api.vxtwitter.com/Twitter/status/{twid}",
    ]
    for api_url in endpoints:
        try:
            req = urllib.request.Request(
                api_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw_json = json.loads(resp.read().decode('utf-8'))
                if raw_json and (raw_json.get('status') or raw_json.get('tweet') or raw_json.get('media_extended') or raw_json.get('mediaURLs')):
                    data = raw_json
                    break
        except Exception as e:
            last_err = e

    if not data:
        raise ValueError(f"No se pudo consultar la API de X (Twitter): {last_err or 'Error de conexión'}")

    # Helper function to download CDN media with browser headers
    def _download_media_file(cdn_url: str, out_dest: str):
        cdn_req = urllib.request.Request(
            cdn_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(cdn_req, timeout=25) as cdn_resp, open(out_dest, 'wb') as out_f:
            out_f.write(cdn_resp.read())

    # Format 1: FxTwitter
    if 'tweet' in data or 'status' in data:
        status_obj = data.get('status') or data.get('tweet') or {}
        text = status_obj.get('text') or "Tweet de X"
        author_obj = status_obj.get('author') or {}
        author = author_obj.get('name') or author_obj.get('screen_name') or user
        media = status_obj.get('media') or {}
        media_all = media.get('all') or []
        if not media_all:
            videos = media.get('videos') or []
            photos = media.get('photos') or []
            media_all = videos + photos
    # Format 2: VxTwitter
    else:
        text = data.get('text') or "Tweet de X"
        author = data.get('user_name') or data.get('user_screen_name') or user
        media_all = []
        for item in data.get('media_extended', []):
            itype = 'video' if item.get('type') in ['video', 'gif'] else 'photo'
            media_all.append({
                'type': itype,
                'url': item.get('url'),
                'thumbnail_url': item.get('thumbnail_url'),
                'duration': item.get('duration_millis', 0) / 1000.0 if item.get('duration_millis') else None,
                'width': (item.get('size') or {}).get('width'),
                'height': (item.get('size') or {}).get('height'),
            })
        if not media_all and data.get('mediaURLs'):
            for u in data['mediaURLs']:
                media_all.append({'type': 'photo', 'url': u})

    if not media_all:
        raise ValueError("Este Tweet no contiene videos ni imágenes descargables.")

    # MP3 Audio
    if format_type == 'mp3':
        video_items = [item for item in media_all if item.get('type') == 'video']
        if not video_items:
            raise ValueError("Esta publicación de X no contiene video para extraer audio.")
        vid_item = video_items[0]
        vid_url = vid_item.get('url')
        if not vid_url:
            raise ValueError("No se pudo obtener el enlace del video.")

        raw_vid = os.path.join(temp_subfolder, f"tw_{twid}_raw.mp4")
        _download_media_file(vid_url, raw_vid)
        mp3_file = os.path.join(temp_subfolder, f"tw_{twid}.mp3")
        cmd = ['ffmpeg', '-y', '-i', raw_vid, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else raw_vid

        thumb_path = None
        if vid_item.get('thumbnail_url'):
            raw_thumb = os.path.join(temp_subfolder, f"tw_thumb_{twid}.jpg")
            try:
                _download_media_file(vid_item['thumbnail_url'], raw_thumb)
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, final_file, temp_subfolder)
                apply_id3_tags(final_file, text, author, thumb_path)
            except Exception:
                pass

        return {
            'type': 'audio',
            'file_path': final_file,
            'thumbnail_path': thumb_path,
            'title': text,
            'artist': author,
            'duration': int(vid_item.get('duration') or 0) or None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(final_file),
            'platform': 'X (Twitter)',
            'platform_emoji': '🐦',
            'is_audio': True,
            'format': 'mp3',
        }

    # Video or Photos
    downloaded_items = []
    for idx, item in enumerate(media_all[:10]):
        itype = item.get('type')
        iurl = item.get('url')
        if not iurl:
            continue
        if itype == 'video':
            out_path = os.path.join(temp_subfolder, f"tw_{twid}_{idx}.mp4")
            _download_media_file(iurl, out_path)
            t_path = None
            if item.get('thumbnail_url'):
                raw_thumb = os.path.join(temp_subfolder, f"tw_thumb_{twid}_{idx}.jpg")
                try:
                    _download_media_file(item['thumbnail_url'], raw_thumb)
                    t_path = convert_thumbnail_to_jpg(raw_thumb, out_path, temp_subfolder)
                except Exception:
                    pass
            downloaded_items.append({
                'type': 'video',
                'file_path': out_path,
                'thumbnail_path': t_path,
                'duration': int(item.get('duration') or 0) or None,
                'width': item.get('width'),
                'height': item.get('height'),
                'filesize': os.path.getsize(out_path),
            })
        elif itype in ['photo', 'image']:
            out_path = os.path.join(temp_subfolder, f"tw_{twid}_{idx}.jpg")
            _download_media_file(iurl, out_path)
            downloaded_items.append({
                'type': 'photo',
                'file_path': out_path,
                'thumbnail_path': None,
                'duration': None,
                'width': item.get('width'),
                'height': item.get('height'),
                'filesize': os.path.getsize(out_path),
            })

    if not downloaded_items:
        raise ValueError("No se pudo descargar ningún medio del Tweet.")

    if len(downloaded_items) == 1:
        single = downloaded_items[0]
        single['title'] = text
        single['artist'] = author
        single['platform'] = 'X (Twitter)'
        single['platform_emoji'] = '🐦'
        single['is_audio'] = False
        single['format'] = 'jpg' if single.get('type') == 'photo' else 'mp4'
        return single

    return {
        'type': 'carousel',
        'media_items': downloaded_items,
        'title': text,
        'artist': author,
        'duration': None,
        'filesize': sum(i['filesize'] for i in downloaded_items),
        'platform': 'X (Twitter)',
        'platform_emoji': '🐦',
        'is_audio': False,
        'format': 'mp4',
    }


# ==============================================================================
# 3. INSTAGRAM ZERO-COOKIES EXTRACTOR (Polaris GraphQL + curl_cffi TLS + Bridge)
# ==============================================================================
def download_instagram_zero_cookies(url: str, temp_subfolder: str, format_type: str = "mp4") -> Dict[str, Any]:
    """
    Downloads Instagram media without requiring any cookies or session ID.
    Uses browser TLS impersonation (Chrome 124) to query Polaris web GraphQL or public bridge.
    """
    shortcode = extract_instagram_shortcode(url)
    if not shortcode:
        raise ValueError("URL de Instagram no válida.")

    # Convert shortcode to media_id
    ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    media_id = 0
    for char in shortcode:
        if char in ALPHABET:
            media_id = media_id * 64 + ALPHABET.index(char)
    media_id_str = str(media_id)

    media_data = None
    caption = "Instagram Post"
    author = "Instagram"

    # Layer 1: Polaris Logged-Out Web GraphQL with curl_cffi
    if HAS_CURL_CFFI:
        try:
            session = cffi_requests.Session(impersonate="chrome124")
            # Step 1: visit homepage to get fresh anonymous csrftoken & datr
            home_resp = session.get("https://www.instagram.com/", timeout=10)
            csrf_token = session.cookies.get("csrftoken", "")

            # Extract LSD token
            lsd_match = re.search(r'\"LSD\",\[\],\{\"token\":\"([^\"]+)\"\}', home_resp.text)
            lsd_token = lsd_match.group(1) if lsd_match else "AVr"

            headers = {
                'X-IG-App-ID': '936619743392459',
                'X-FB-Friendly-Name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
                'X-CSRFToken': csrf_token,
                'X-FB-LSD': lsd_token,
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': f'https://www.instagram.com/reel/{shortcode}/',
            }

            payload = {
                'lsd': lsd_token,
                'fb_api_caller_class': 'RelayModern',
                'fb_api_req_friendly_name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
                'server_timestamps': 'true',
                'variables': json.dumps({'media_id': media_id_str}, separators=(',', ':')),
                'doc_id': '28256812867323632',
            }

            gql_resp = session.post(
                "https://www.instagram.com/api/graphql",
                headers=headers,
                data=payload,
                timeout=15
            )

            if gql_resp.status_code == 200:
                gql_json = gql_resp.json()
                media_node = (gql_json.get('data') or {}).get('xig_polaris_media') or {}
                product = media_node.get('if_not_gated_logged_out')
                if product:
                    media_data = product
                    caption = (product.get('caption') or {}).get('text') or caption
                    author = (product.get('user') or {}).get('username') or author
                    logger.info("✅ Instagram extraído exitosamente vía Polaris GraphQL (0 Cookies).")
        except Exception as e_polaris:
            logger.warning(f"Intento Polaris GraphQL no obtuvo el medio: {e_polaris}")

    # Layer 2: Cobalt Public Bridge
    if not media_data:
        for instance in COBALT_INSTANCES:
            try:
                c_req = urllib.request.Request(
                    instance,
                    data=json.dumps({"url": url}).encode(),
                    headers={
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                        'User-Agent': 'Mozilla/5.0'
                    }
                )
                with urllib.request.urlopen(c_req, timeout=12) as c_resp:
                    c_data = json.loads(c_resp.read().decode())
                    if c_data.get('status') in ['redirect', 'tunnel'] and c_data.get('url'):
                        c_url = c_data['url']
                        out_vid = os.path.join(temp_subfolder, f"ig_{shortcode}.mp4")
                        urllib.request.urlretrieve(c_url, out_vid)
                        thumb_file = convert_thumbnail_to_jpg(None, out_vid, temp_subfolder)
                        return {
                            'type': 'video',
                            'file_path': out_vid,
                            'thumbnail_path': thumb_file,
                            'title': "Publicación de Instagram",
                            'artist': "Instagram",
                            'duration': None,
                            'width': None,
                            'height': None,
                            'filesize': os.path.getsize(out_vid),
                            'platform': 'Instagram',
                            'platform_emoji': '📸',
                            'is_audio': False,
                            'format': 'mp4',
                        }
            except Exception:
                continue

    # Layer 3: Web OpenGraph Scraper (extracts public Instagram photos)
    if not media_data and HAS_CURL_CFFI:
        try:
            s_ig = cffi_requests.Session(impersonate="chrome124")
            resp_og = s_ig.get(url, timeout=12, allow_redirects=True)
            if resp_og.status_code == 200:
                html_txt = resp_og.text
                img_m = re.search(r'[\"\']og:image[\"\']\s*content=[\"\']([^\'\"]+)[\"\']', html_txt)
                if not img_m:
                    img_m = re.search(r'content=[\"\']([^\'\"]+)[\"\']\s*property=[\"\']og:image[\"\']', html_txt)

                title_m = re.search(r'[\"\']og:title[\"\']\s*content=[\"\']([^\'\"]+)[\"\']', html_txt)
                if not title_m:
                    title_m = re.search(r'content=[\"\']([^\'\"]+)[\"\']\s*property=[\"\']og:title[\"\']', html_txt)
                ig_title = html.unescape(title_m.group(1)) if title_m else "Foto de Instagram"

                if img_m:
                    direct_img = html.unescape(img_m.group(1))
                    out_p = os.path.join(temp_subfolder, f"ig_{shortcode}.jpg")
                    r_dl = s_ig.get(direct_img, timeout=15)
                    with open(out_p, 'wb') as f:
                        f.write(r_dl.content)
                    if os.path.exists(out_p) and os.path.getsize(out_p) > 0:
                        logger.info("✅ Foto de Instagram extraída vía OpenGraph (Zero-Cookies).")
                        return {
                            'type': 'photo',
                            'file_path': out_p,
                            'thumbnail_path': None,
                            'title': ig_title,
                            'artist': "Instagram",
                            'duration': None,
                            'width': None,
                            'height': None,
                            'filesize': os.path.getsize(out_p),
                            'platform': 'Instagram',
                            'platform_emoji': '📸',
                            'is_audio': False,
                            'format': 'jpg',
                        }
        except Exception as e_og:
            logger.debug(f"Instagram OpenGraph scraper falló: {e_og}")

    if not media_data:
        raise ValueError(
            "No se pudo extraer la publicación de Instagram de forma anónima.\n"
            "Verifica que el enlace sea público."
        )

    # Process extracted media_data
    media_type = media_data.get('media_type')  # 1: Photo, 2: Video, 8: Carousel

    # MP3 Format
    if format_type == 'mp3':
        vid_url = None
        if media_type == 2:
            video_versions = media_data.get('video_versions') or []
            if video_versions:
                vid_url = video_versions[0].get('url')
        elif media_type == 8:
            carousel = media_data.get('carousel_media') or []
            for sub in carousel:
                if sub.get('media_type') == 2:
                    vids = sub.get('video_versions') or []
                    if vids:
                        vid_url = vids[0].get('url')
                        break

        if not vid_url:
            raise ValueError("Esta publicación de Instagram no contiene video para extraer audio.")

        raw_vid = os.path.join(temp_subfolder, f"ig_{shortcode}_raw.mp4")
        urllib.request.urlretrieve(vid_url, raw_vid)
        mp3_file = os.path.join(temp_subfolder, f"ig_{shortcode}.mp3")
        cmd = ['ffmpeg', '-y', '-i', raw_vid, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else raw_vid

        thumb_path = None
        candidates = (media_data.get('image_versions2') or {}).get('candidates') or []
        if candidates:
            raw_thumb = os.path.join(temp_subfolder, "ig_thumb.jpg")
            try:
                urllib.request.urlretrieve(candidates[0]['url'], raw_thumb)
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, final_file, temp_subfolder)
                apply_id3_tags(final_file, caption, author, thumb_path)
            except Exception:
                pass

        return {
            'type': 'audio',
            'file_path': final_file,
            'thumbnail_path': thumb_path,
            'title': caption,
            'artist': author,
            'duration': int(media_data.get('video_duration', 0) or 0) or None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(final_file),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': True,
            'format': 'mp3',
        }

    # Single Photo
    if media_type == 1:
        candidates = (media_data.get('image_versions2') or {}).get('candidates') or []
        if not candidates:
            raise ValueError("No se encontró la imagen de este post de Instagram.")
        out_photo = os.path.join(temp_subfolder, f"ig_{shortcode}.jpg")
        urllib.request.urlretrieve(candidates[0]['url'], out_photo)
        return {
            'type': 'photo',
            'file_path': out_photo,
            'thumbnail_path': None,
            'title': caption,
            'artist': author,
            'duration': None,
            'width': candidates[0].get('width'),
            'height': candidates[0].get('height'),
            'filesize': os.path.getsize(out_photo),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': False,
            'format': 'jpg',
        }

    # Single Video / Reel
    if media_type == 2:
        video_versions = media_data.get('video_versions') or []
        if not video_versions:
            raise ValueError("No se encontró el archivo de video en este Reel.")
        out_vid = os.path.join(temp_subfolder, f"ig_{shortcode}.mp4")
        urllib.request.urlretrieve(video_versions[0]['url'], out_vid)

        thumb_path = None
        candidates = (media_data.get('image_versions2') or {}).get('candidates') or []
        if candidates:
            raw_thumb = os.path.join(temp_subfolder, "ig_thumb.jpg")
            try:
                urllib.request.urlretrieve(candidates[0]['url'], raw_thumb)
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, out_vid, temp_subfolder)
            except Exception:
                pass

        return {
            'type': 'video',
            'file_path': out_vid,
            'thumbnail_path': thumb_path,
            'title': caption,
            'artist': author,
            'duration': int(media_data.get('video_duration', 0) or 0) or None,
            'width': video_versions[0].get('width'),
            'height': video_versions[0].get('height'),
            'filesize': os.path.getsize(out_vid),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': False,
            'format': 'mp4',
        }

    # Carousel (multiple photos / videos)
    if media_type == 8:
        carousel = media_data.get('carousel_media') or []
        downloaded = []
        for idx, sub in enumerate(carousel[:10]):
            sub_type = sub.get('media_type')
            if sub_type == 2:
                vids = sub.get('video_versions') or []
                if vids:
                    out_path = os.path.join(temp_subfolder, f"ig_{shortcode}_{idx}.mp4")
                    urllib.request.urlretrieve(vids[0]['url'], out_path)
                    t_cand = (sub.get('image_versions2') or {}).get('candidates') or []
                    t_path = None
                    if t_cand:
                        raw_t = os.path.join(temp_subfolder, f"ig_thumb_{shortcode}_{idx}.jpg")
                        try:
                            urllib.request.urlretrieve(t_cand[0]['url'], raw_t)
                            t_path = convert_thumbnail_to_jpg(raw_t, out_path, temp_subfolder)
                        except Exception:
                            pass
                    downloaded.append({
                        'type': 'video',
                        'file_path': out_path,
                        'thumbnail_path': t_path,
                        'duration': int(sub.get('video_duration', 0) or 0) or None,
                        'width': vids[0].get('width'),
                        'height': vids[0].get('height'),
                        'filesize': os.path.getsize(out_path),
                    })
            elif sub_type == 1:
                imgs = (sub.get('image_versions2') or {}).get('candidates') or []
                if imgs:
                    out_path = os.path.join(temp_subfolder, f"ig_{shortcode}_{idx}.jpg")
                    urllib.request.urlretrieve(imgs[0]['url'], out_path)
                    downloaded.append({
                        'type': 'photo',
                        'file_path': out_path,
                        'thumbnail_path': None,
                        'duration': None,
                        'width': imgs[0].get('width'),
                        'height': imgs[0].get('height'),
                        'filesize': os.path.getsize(out_path),
                    })

        if not downloaded:
            raise ValueError("No se pudieron descargar los elementos del carrusel.")

        return {
            'type': 'carousel',
            'media_items': downloaded,
            'title': caption,
            'artist': author,
            'duration': None,
            'filesize': sum(i['filesize'] for i in downloaded),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': False,
            'format': 'mp4',
        }

    raise ValueError("Formato multimedia de Instagram no soportado.")


# ==============================================================================
# 3.1. INSTAGRAM AUTHENTICATED EXTRACTOR (instagrapi Client)
# ==============================================================================
_instagrapi_client = None

def get_instagrapi_client():
    """Initializes or retrieves singleton instagrapi client with session persistence."""
    global _instagrapi_client
    if _instagrapi_client is not None:
        return _instagrapi_client

    try:
        from instagrapi import Client
    except ImportError:
        logger.warning("instagrapi no está disponible en este entorno.")
        return None

    cl = Client()
    session_file = os.path.join(tempfile.gettempdir(), "instagrapi_session.json")

    # 1. Intentar cargar sesión en caché
    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            logger.info("✅ Sesión de Instagram cargada desde caché local.")
            _instagrapi_client = cl
            return cl
        except Exception as e:
            logger.warning(f"Error cargando sesión previa de Instagram: {e}")

    # 2. Login con SESSIONID
    if INSTAGRAM_SESSIONID:
        try:
            cl.login_by_sessionid(INSTAGRAM_SESSIONID)
            cl.dump_settings(session_file)
            logger.info("✅ Sesión de Instagram iniciada exitosamente vía INSTAGRAM_SESSIONID.")
            _instagrapi_client = cl
            return cl
        except Exception as e:
            logger.error(f"Fallo login con INSTAGRAM_SESSIONID: {e}")

    # 3. Login con Usuario y Contraseña
    if IG_USERNAME and IG_PASSWORD:
        try:
            cl.login(IG_USERNAME, IG_PASSWORD)
            cl.dump_settings(session_file)
            logger.info(f"✅ Sesión de Instagram iniciada exitosamente con cuenta @{IG_USERNAME}.")
            _instagrapi_client = cl
            return cl
        except Exception as e:
            logger.error(f"Fallo login con usuario y contraseña de Instagram: {e}")

    return None


def download_instagram_authenticated(url: str, temp_subfolder: str, format_type: str = "mp4") -> Dict[str, Any]:
    """Downloads any Instagram Reel, Video, Photo or Album using instagrapi."""
    cl = get_instagrapi_client()
    if not cl:
        raise ValueError("No se pudo iniciar sesión con la cuenta de servicio de Instagram.")

    media_pk = cl.media_pk_from_url(url)
    info = cl.media_info(media_pk)

    title = info.caption_text or "Reel de Instagram"
    author = (info.user.username if info.user else None) or "Instagram"
    duration = info.video_duration or None

    if format_type == 'mp3':
        if info.media_type not in (2, 8):
            raise ValueError("Esta publicación de Instagram es una imagen y no contiene video ni audio.")
        raw_vid = cl.video_download(media_pk, temp_subfolder)
        mp3_file = os.path.join(temp_subfolder, f"ig_{media_pk}.mp3")
        cmd = ['ffmpeg', '-y', '-i', str(raw_vid), '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else str(raw_vid)
        thumb_path = None
        if info.thumbnail_url:
            raw_thumb = os.path.join(temp_subfolder, "ig_thumb.jpg")
            try:
                t_req = urllib.request.Request(str(info.thumbnail_url), headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(t_req, timeout=12) as t_resp, open(raw_thumb, 'wb') as t_f:
                    t_f.write(t_resp.read())
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, final_file, temp_subfolder)
                apply_id3_tags(final_file, title, author, thumb_path)
            except Exception:
                pass
        return {
            'type': 'audio',
            'file_path': final_file,
            'thumbnail_path': thumb_path,
            'title': title,
            'artist': author,
            'duration': int(duration) if duration else None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(final_file),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': True,
            'format': 'mp3',
        }

    # Carousel / Album (media_type == 8)
    if info.media_type == 8:
        album_paths = cl.album_download(media_pk, temp_subfolder)
        media_items = []
        for p in album_paths:
            p_str = str(p)
            is_vid = p_str.lower().endswith(('.mp4', '.mov'))
            media_items.append({
                'type': 'video' if is_vid else 'photo',
                'file_path': p_str,
                'thumbnail_path': None,
                'filesize': os.path.getsize(p_str),
            })
        return {
            'type': 'carousel',
            'media_items': media_items,
            'title': title,
            'artist': author,
            'duration': None,
            'filesize': sum(i['filesize'] for i in media_items),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': False,
            'format': 'mp4',
        }

    # Photo (media_type == 1)
    if info.media_type == 1:
        photo_path = cl.photo_download(media_pk, temp_subfolder)
        return {
            'type': 'photo',
            'file_path': str(photo_path),
            'thumbnail_path': None,
            'title': title,
            'artist': author,
            'duration': None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(str(photo_path)),
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': False,
            'format': 'jpg',
        }

    # Video / Reel
    vid_path = cl.video_download(media_pk, temp_subfolder)
    thumb_path = None
    if info.thumbnail_url:
        raw_thumb = os.path.join(temp_subfolder, "ig_thumb.jpg")
        try:
            t_req = urllib.request.Request(str(info.thumbnail_url), headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(t_req, timeout=12) as t_resp, open(raw_thumb, 'wb') as t_f:
                t_f.write(t_resp.read())
            thumb_path = convert_thumbnail_to_jpg(raw_thumb, str(vid_path), temp_subfolder)
        except Exception:
            pass

    return {
        'type': 'video',
        'file_path': str(vid_path),
        'thumbnail_path': thumb_path,
        'title': title,
        'artist': author,
        'duration': int(duration) if duration else None,
        'width': None,
        'height': None,
        'filesize': os.path.getsize(str(vid_path)),
        'platform': 'Instagram',
        'platform_emoji': '📸',
        'is_audio': False,
        'format': 'mp4',
    }


# ==============================================================================
# 4. YOUTUBE ZERO-COOKIES & BRIDGE EXTRACTOR
# ==============================================================================
def download_youtube_bridge(url: str, format_type: str, temp_subfolder: str) -> Optional[Dict[str, Any]]:
    """Attempts to download YouTube audio/video via public bridge API to prevent Render IP blocks."""
    yt_id = extract_youtube_id(url)
    if not yt_id:
        return None

    for instance in COBALT_INSTANCES:
        try:
            payload = {
                "url": f"https://www.youtube.com/watch?v={yt_id}",
                "downloadMode": "audio" if format_type == "mp3" else "auto",
                "audioFormat": "mp3" if format_type == "mp3" else "best",
            }
            req = urllib.request.Request(
                instance,
                data=json.dumps(payload).encode(),
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                if data.get('status') in ['redirect', 'tunnel'] and data.get('url'):
                    stream_url = data['url']
                    ext = "mp3" if format_type == "mp3" else "mp4"
                    out_file = os.path.join(temp_subfolder, f"yt_{yt_id}.{ext}")
                    urllib.request.urlretrieve(stream_url, out_file)
                    thumb_path = None
                    if ext == "mp4":
                        thumb_path = convert_thumbnail_to_jpg(None, out_file, temp_subfolder)
                    logger.info(f"✅ Descarga de YouTube completada exitosamente vía Bridge ({instance})")
                    return {
                        'type': 'audio' if format_type == 'mp3' else 'video',
                        'file_path': out_file,
                        'thumbnail_path': thumb_path,
                        'title': f"YouTube Video {yt_id}",
                        'artist': "YouTube",
                        'duration': None,
                        'width': None,
                        'height': None,
                        'filesize': os.path.getsize(out_file),
                        'platform': 'YouTube',
                        'platform_emoji': '▶️',
                        'is_audio': (format_type == 'mp3'),
                        'format': format_type,
                    }
        except Exception as e:
            logger.debug(f"Bridge attempt on {instance} failed: {e}")
            continue

    return None


# ==============================================================================
# 5. FACEBOOK ZERO-COOKIES EXTRACTOR (yt-dlp for Videos + curl_cffi for Photos)
# ==============================================================================
def download_facebook_fast(url: str, temp_subfolder: str, format_type: str = "mp4") -> Dict[str, Any]:
    """
    Downloads Facebook media (Reels, Videos, Photos and Posts).
    1. If the URL is a video/reel or format is mp3, tries yt-dlp first.
    2. If yt-dlp fails or URL is a photo, scrapes OpenGraph media via curl_cffi.
    """
    is_video_hint = any(k in url.lower() for k in ['/reel/', '/watch', 'fb.watch', '/videos/'])

    # 1. Try yt-dlp for videos
    if is_video_hint or format_type == 'mp3':
        try:
            ydl_opts = {
                'outtmpl': os.path.join(temp_subfolder, 'fb_%(id)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'max_filesize': 120 * 1024 * 1024,
                'socket_timeout': 15,
            }
            if format_type == 'mp3':
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            else:
                ydl_opts['format'] = 'best[ext=mp4]/best'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    filename = ydl.prepare_filename(info)
                    if format_type == 'mp3':
                        cand = os.path.splitext(filename)[0] + '.mp3'
                        final_file = cand if os.path.exists(cand) else filename
                    else:
                        cand = os.path.splitext(filename)[0] + '.mp4'
                        final_file = cand if os.path.exists(cand) else filename

                    if os.path.exists(final_file) and os.path.getsize(final_file) > 0:
                        thumb = convert_thumbnail_to_jpg(None, final_file, temp_subfolder)
                        return {
                            'type': 'audio' if format_type == 'mp3' else 'video',
                            'file_path': final_file,
                            'thumbnail_path': thumb,
                            'title': info.get('title') or "Video de Facebook",
                            'artist': info.get('uploader') or "Facebook",
                            'duration': int(info.get('duration') or 0) or None,
                            'width': info.get('width'),
                            'height': info.get('height'),
                            'filesize': os.path.getsize(final_file),
                            'platform': 'Facebook',
                            'platform_emoji': '👥',
                            'is_audio': (format_type == 'mp3'),
                            'format': format_type,
                        }
        except Exception as e_fb_vid:
            logger.info(f"yt-dlp no extrajo video de Facebook ({e_fb_vid}), intentando extractor de fotos...")

    # 2. Extract Photo / Post using curl_cffi TLS impersonation
    try:
        session = cffi_requests.Session(impersonate="chrome124") if HAS_CURL_CFFI else None
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
        }
        if session:
            resp = session.get(url, headers=headers, timeout=15, allow_redirects=True)
            text = resp.text
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                text = r.read().decode('utf-8', errors='ignore')

        # Check og:image
        img_m = re.search(r'[\"\']og:image[\"\']\s*content=[\"\']([^\'\"]+)[\"\']', text)
        if not img_m:
            img_m = re.search(r'content=[\"\']([^\'\"]+)[\"\']\s*property=[\"\']og:image[\"\']', text)
        if not img_m:
            img_m = re.search(r'[\"\']twitter:image[\"\']\s*content=[\"\']([^\'\"]+)[\"\']', text)

        # Check title
        title_m = re.search(r'[\"\']og:title[\"\']\s*content=[\"\']([^\'\"]+)[\"\']', text)
        if not title_m:
            title_m = re.search(r'content=[\"\']([^\'\"]+)[\"\']\s*property=[\"\']og:title[\"\']', text)
        title = html.unescape(title_m.group(1)) if title_m else "Foto de Facebook"

        # Check description
        desc_m = re.search(r'[\"\']og:description[\"\']\s*content=[\"\']([^\'\"]+)[\"\']', text)
        if not desc_m:
            desc_m = re.search(r'content=[\"\']([^\'\"]+)[\"\']\s*property=[\"\']og:description[\"\']', text)
        desc = html.unescape(desc_m.group(1)) if desc_m else ""
        if desc and len(desc) > len(title) and title == "Foto de Facebook":
            title = desc[:200]

        if img_m:
            img_url = html.unescape(img_m.group(1))
            out_photo = os.path.join(temp_subfolder, "fb_photo.jpg")
            if session:
                r_img = session.get(img_url, timeout=20)
                with open(out_photo, 'wb') as f:
                    f.write(r_img.content)
            else:
                cdn_req = urllib.request.Request(img_url, headers=headers)
                with urllib.request.urlopen(cdn_req, timeout=20) as cdn_r, open(out_photo, 'wb') as f:
                    f.write(cdn_r.read())

            if os.path.exists(out_photo) and os.path.getsize(out_photo) > 0:
                logger.info(f"✅ Foto de Facebook descargada exitosamente ({os.path.getsize(out_photo)} bytes)")
                return {
                    'type': 'photo',
                    'file_path': out_photo,
                    'thumbnail_path': None,
                    'title': title,
                    'artist': "Facebook",
                    'duration': None,
                    'width': None,
                    'height': None,
                    'filesize': os.path.getsize(out_photo),
                    'platform': 'Facebook',
                    'platform_emoji': '👥',
                    'is_audio': False,
                    'format': 'jpg',
                }
    except Exception as e_fb_photo:
        logger.warning(f"Error extrayendo foto de Facebook: {e_fb_photo}")

    raise ValueError(
        "🔒 Esta publicación de Facebook es privada, restringida o requiere inicio de sesión en Facebook.\n"
        "Verifica que la publicación sea pública."
    )


# ==============================================================================
# MAIN VIDEODOWNLOADER CLASS
# ==============================================================================
class VideoDownloader:
    def __init__(self, temp_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        self.temp_dir = temp_dir or tempfile.gettempdir()
        self.cookies_file = cookies_file

    def _sync_download_spotify(self, url: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """Downloads high quality audio matching a Spotify track using SoundCloud (primary) or YouTube Bridge."""
        meta = get_spotify_info(url)
        title = meta['title']
        artist = meta['artist']
        search_query = f"{artist} {title}" if artist and artist != 'Artista' else title

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

        info = None
        ydl_used = None

        # 1. Primary: SoundCloud search (0 IP blocks on cloud servers!)
        try:
            ydl_sc = yt_dlp.YoutubeDL(ydl_opts)
            sc_info = ydl_sc.extract_info(f"scsearch1:{search_query}", download=True)
            if sc_info and 'entries' in sc_info and sc_info['entries']:
                info = sc_info
                ydl_used = ydl_sc
                logger.info(f"✅ Audio de Spotify localizado en SoundCloud: '{search_query}'")
        except Exception as e:
            logger.info(f"Búsqueda SoundCloud falló: {e}")

        # 2. Fallback: YouTube Search with rotating clients
        if not info or not info.get('entries'):
            try:
                yt_opts = dict(ydl_opts)
                yt_opts['extractor_args'] = {'youtube': {'player_client': ['visionos', 'android_vr']}}
                ydl_yt = yt_dlp.YoutubeDL(yt_opts)
                yt_info = ydl_yt.extract_info(f"ytsearch1:{artist} - {title} audio", download=True)
                if yt_info and 'entries' in yt_info and yt_info['entries']:
                    info = yt_info
                    ydl_used = ydl_yt
            except Exception as e2:
                logger.warning(f"Fallback YouTube falló: {e2}")

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

        # Download official Spotify cover art and embed into MP3 with mutagen
        thumb_file = None
        if meta.get('thumbnail_url'):
            thumb_file = os.path.join(temp_subfolder, 'spotify_cover.jpg')
            try:
                urllib.request.urlretrieve(meta['thumbnail_url'], thumb_file)
                apply_id3_tags(mp3_file, title, artist, thumb_file)
            except Exception:
                thumb_file = None

        return {
            'type': 'audio',
            'file_path': mp3_file,
            'thumbnail_path': thumb_file,
            'title': title,
            'artist': artist,
            'duration': int(entry.get('duration') or 0) or None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(mp3_file),
            'platform': 'Spotify',
            'platform_emoji': '🟢',
            'is_audio': True,
            'format': 'mp3',
        }

    def _sync_download(self, url: str, format_type: str, output_template: str, temp_subfolder: str) -> Dict[str, Any]:
        """Synchronous dispatcher for all platforms with Zero-Cookies priority."""
        format_type = format_type.lower().strip()
        if format_type not in ['mp3', 'mp4']:
            format_type = 'mp4'

        platform_name, platform_emoji = detect_platform(url)

        # 1. Spotify
        if platform_name == "Spotify":
            return self._sync_download_spotify(url, output_template, temp_subfolder)

        # 2. TikTok (Primary: TikWM API)
        if platform_name == "TikTok":
            try:
                return download_tiktok_fast(url, format_type, temp_subfolder)
            except Exception as e_tt:
                logger.warning(f"TikWM falló ({e_tt}), intentando yt-dlp como fallback...")

        # 3. X / Twitter (Primary: FxTwitter / VxTwitter API)
        if platform_name in ("X (Twitter)", "Twitter"):
            try:
                return download_twitter_fast(url, temp_subfolder, format_type=format_type)
            except Exception as e_tw:
                if "no contiene videos ni imágenes" in str(e_tw):
                    raise
                logger.warning(f"APIs de X fallaron ({e_tw}), intentando yt-dlp como fallback...")

        # 4. Instagram
        if platform_name == "Instagram":
            norm_url = normalize_instagram_url(url)
            # 1. Si hay credenciales de cuenta secundaria/fantasma, usar instagrapi
            if INSTAGRAM_SESSIONID or (IG_USERNAME and IG_PASSWORD):
                try:
                    logger.info("🔑 Descargando Instagram mediante cuenta de servicio (instagrapi)...")
                    return download_instagram_authenticated(norm_url, temp_subfolder, format_type=format_type)
                except Exception as e_auth:
                    logger.warning(f"Error en cuenta de servicio IG ({e_auth}), probando Polaris Zero-Cookies...")

            # 2. De lo contrario o como fallback, usar Polaris Zero-Cookies
            try:
                return download_instagram_zero_cookies(norm_url, temp_subfolder, format_type=format_type)
            except Exception as e_ig:
                logger.warning(f"Instagram Zero-Cookies no pudo extraer el medio: {e_ig}")
                raise ValueError(
                    "🔒 Este contenido de Instagram es privado, restringido por edad o requiere inicio de sesión en su plataforma.\n"
                    "El bot opera con arquitectura Zero-Cookies y solo puede descargar contenido accesible públicamente."
                )

        # 5. Facebook (Reels, Videos, Photos via yt-dlp + curl_cffi OpenGraph)
        if platform_name == "Facebook":
            return download_facebook_fast(url, temp_subfolder, format_type=format_type)

        # 6. YouTube (Bridge attempt first on cloud datacenter IPs)
        if platform_name in ("YouTube", "YouTube Music"):
            bridge_res = download_youtube_bridge(url, format_type, temp_subfolder)
            if bridge_res:
                return bridge_res

        # 6. General Engine (yt-dlp with rotating clients, cookies if present)
        ydl_opts: Dict[str, Any] = {
            'outtmpl': output_template,
            'writethumbnail': True,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'max_filesize': 120 * 1024 * 1024,
            'concurrent_fragment_downloads': 8,
            'socket_timeout': 15,
            'http_chunk_size': 10485760,
            'retries': 3,
            'fragment_retries': 5,
        }

        # Soporte de Proxy para yt-dlp (evita bloqueos de IP en centros de datos como Render)
        proxy = os.getenv("YTDL_PROXY") or os.getenv("HTTP_PROXY")
        if proxy:
            ydl_opts['proxy'] = proxy.strip()

        if self.cookies_file and os.path.exists(self.cookies_file):
            ydl_opts['cookiefile'] = self.cookies_file

        if format_type == 'mp3':
            ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            ydl_opts['format'] = (
                'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/'
                'bestvideo[height<=720]+bestaudio/'
                'best[height<=720][ext=mp4]/'
                'best[height<=720]/'
                'best'
            )
            ydl_opts['merge_output_format'] = 'mp4'

        is_yt = is_youtube_url(url)
        attempts = []
        if is_yt:
            if self.cookies_file and os.path.exists(self.cookies_file):
                attempts.append(("con cookies", None, True))
            attempts.append(("ios", ['ios'], False))
            attempts.append(("android", ['android'], False))
            attempts.append(("mweb", ['mweb'], False))
            attempts.append(("visionos", ['visionos'], False))
            attempts.append(("android_vr", ['android_vr'], False))
            attempts.append(("tv", ['tv'], False))
            attempts.append(("web", ['web'], False))
        else:
            attempts.append(("default", None, bool(self.cookies_file)))

        info = None
        active_ydl = None
        last_error = None

        for attempt_name, client, use_cookies in attempts:
            attempt_opts = dict(ydl_opts)
            if not use_cookies:
                attempt_opts.pop('cookiefile', None)
            if client:
                attempt_opts['extractor_args'] = {'youtube': {'player_client': client}}
                if any(c in ('android', 'ios', 'visionos', 'android_vr', 'tv', 'mweb') for c in client):
                    attempt_opts.pop('cookiefile', None)

            try:
                active_ydl = yt_dlp.YoutubeDL(attempt_opts)
                info = active_ydl.extract_info(url, download=True)
                if info:
                    logger.info(f"✅ Descarga completada usando estrategia: {attempt_name}")
                    break
            except Exception as e:
                last_error = e
                logger.debug(f"Attempt {attempt_name} falló: {e}")
                continue

        if not info:
            raw_err = str(last_error or 'Error de extracción')
            if any(k in raw_err.lower() for k in ['cookies', 'empty media response', 'login', 'private', 'sign in', 'confirm your age']):
                clean_err = "Este contenido es privado, restringido o requiere inicio de sesión en la plataforma."
            else:
                clean_err = raw_err
            raise ValueError(f"No se pudo descargar el contenido: {clean_err}")

        if 'entries' in info and info['entries']:
            video_entries = [
                e for e in info['entries']
                if e and (
                    e.get('vcodec') not in (None, 'none')
                    or e.get('ext') in ['mp4', 'mov', 'mkv', 'webm']
                    or e.get('video_ext') not in (None, 'none')
                )
            ]
            info = video_entries[0] if video_entries else info['entries'][0]

        title = info.get('title', 'Video')
        uploader = info.get('uploader') or info.get('channel') or platform_name
        duration = info.get('duration', 0) or 0
        width = info.get('width')
        height = info.get('height')

        base_filename = active_ydl.prepare_filename(info) if active_ydl else ""
        result_file = None

        if format_type == 'mp3':
            mp3_cand = os.path.splitext(base_filename)[0] + '.mp3'
            if os.path.exists(mp3_cand) and os.path.getsize(mp3_cand) > 0:
                result_file = mp3_cand
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
                for f in os.listdir(temp_subfolder):
                    if f.lower().endswith(('.mp4', '.mkv', '.webm', '.mov')) and os.path.getsize(os.path.join(temp_subfolder, f)) > 0:
                        result_file = os.path.join(temp_subfolder, f)
                        break

        if not result_file or not os.path.exists(result_file):
            raise FileNotFoundError("No se encontró el archivo multimedia descargado.")

        file_size = os.path.getsize(result_file)

        # Auto-compression if slightly over 50MB
        if format_type == 'mp4' and file_size > MAX_TELEGRAM_SIZE_BYTES:
            if duration and 0 < duration < 1800:
                compressed_path = os.path.join(temp_subfolder, 'compressed_video.mp4')
                if compress_video_ffmpeg(result_file, compressed_path, float(duration)):
                    result_file = compressed_path
                    file_size = os.path.getsize(result_file)

        if file_size > MAX_TELEGRAM_SIZE_BYTES:
            raise ValueError(
                f"El archivo pesa {file_size / (1024 * 1024):.1f} MB, "
                f"superando el límite de 50 MB de Telegram."
            )

        # Convert thumbnail
        raw_thumb = None
        thumb_prefix = os.path.splitext(base_filename)[0]
        for ext in ['.jpg', '.jpeg', '.webp', '.png']:
            t_cand = thumb_prefix + ext
            if os.path.exists(t_cand):
                raw_thumb = t_cand
                break

        final_thumb = convert_thumbnail_to_jpg(raw_thumb, result_file, temp_subfolder)

        if format_type == 'mp3':
            apply_id3_tags(result_file, title, uploader, final_thumb)

        return {
            'type': 'audio' if (format_type == 'mp3') else 'video',
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
        """Asynchronously downloads media into an isolated temporary directory."""
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
