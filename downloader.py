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
    audio_bitrate = 96 * 1000 if duration > 600 else 128 * 1000
    video_bitrate = max(total_bitrate - audio_bitrate, 100 * 1000)

    try:
        cmd = [
            'ffmpeg', '-y', '-i', input_file,
            '-c:v', 'libx264',
            '-b:v', str(video_bitrate),
            '-maxrate', str(int(video_bitrate * 1.3)),
            '-bufsize', str(int(video_bitrate * 2)),
            '-preset', 'veryfast',
            '-c:a', 'aac',
            '-b:a', '96k' if duration > 600 else '128k',
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


def download_tiktok_via_api(url: str, format_type: str, temp_subfolder: str) -> Dict[str, Any]:
    """Downloads TikTok video or audio directly via TikWM API when yt-dlp is IP blocked."""
    import urllib.parse
    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        )
    }
    req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    if data.get('code') != 0:
        msg = data.get('msg', 'Error desconocido en TikTok API')
        raise ValueError(f"No se pudo descargar el video de TikTok: {msg}")

    item = data.get('data', {})
    title = item.get('title') or "Video de TikTok"
    author = item.get('author', {}).get('nickname') or item.get('author', {}).get('unique_id') or "TikTok"
    duration = item.get('duration') or 0
    cover_url = item.get('cover')

    if format_type == 'mp3':
        music_url = item.get('music') or item.get('play')
        if not music_url:
            raise ValueError("No se encontró el audio de este TikTok.")
        raw_audio = os.path.join(temp_subfolder, "tiktok_audio_raw")
        urllib.request.urlretrieve(music_url, raw_audio)
        mp3_file = os.path.join(temp_subfolder, f"{item.get('id', 'tiktok_audio')}.mp3")
        cmd = ['ffmpeg', '-y', '-i', raw_audio, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else raw_audio
        thumb_file = None
        if cover_url:
            raw_cov = os.path.join(temp_subfolder, "tiktok_cover.jpg")
            try:
                urllib.request.urlretrieve(cover_url, raw_cov)
                thumb_file = convert_thumbnail_to_jpg(raw_cov, "", temp_subfolder)
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
            if len(media_items) == 1:
                return {
                    'type': 'photo',
                    'file_path': media_items[0]['file_path'],
                    'thumbnail_path': None,
                    'title': title,
                    'artist': author,
                    'duration': None,
                    'width': None,
                    'height': None,
                    'filesize': media_items[0]['filesize'],
                    'platform': 'TikTok',
                    'platform_emoji': '🎵',
                    'is_audio': False,
                    'format': 'jpg',
                }
            return {
                'type': 'album',
                'media_items': media_items,
                'title': title,
                'artist': author,
                'duration': None,
                'filesize': sum(m['filesize'] for m in media_items),
                'platform': 'TikTok',
                'platform_emoji': '🎵',
                'is_audio': False,
                'format': 'album',
            }

    # Video MP4
    play_url = item.get('play') or item.get('wmplay')
    if not play_url:
        raise ValueError("No se encontró el video de este TikTok.")

    dest_video = os.path.join(temp_subfolder, f"{item.get('id', 'tiktok_video')}.mp4")
    urllib.request.urlretrieve(play_url, dest_video)

    thumb_file = None
    if cover_url:
        raw_cov = os.path.join(temp_subfolder, "tiktok_cover.jpg")
        try:
            urllib.request.urlretrieve(cover_url, raw_cov)
            thumb_file = convert_thumbnail_to_jpg(raw_cov, dest_video, temp_subfolder)
        except Exception:
            pass

    if not thumb_file:
        thumb_file = convert_thumbnail_to_jpg(None, dest_video, temp_subfolder)

    file_size = os.path.getsize(dest_video)
    if file_size > MAX_TELEGRAM_SIZE_BYTES and duration and 0 < duration < 1800:
        compressed_path = os.path.join(temp_subfolder, 'compressed_video.mp4')
        if compress_video_ffmpeg(dest_video, compressed_path, float(duration)):
            dest_video = compressed_path
            file_size = os.path.getsize(dest_video)

    return {
        'type': 'video',
        'file_path': dest_video,
        'thumbnail_path': thumb_file,
        'title': title,
        'artist': author,
        'duration': int(duration) if duration else None,
        'width': None,
        'height': None,
        'filesize': file_size,
        'platform': 'TikTok',
        'platform_emoji': '🎵',
        'is_audio': False,
        'format': 'mp4',
    }


def download_twitter_fallback(url: str, temp_subfolder: str, format_type: str = "mp4") -> Dict[str, Any]:
    """Downloads Twitter/X video, photos, or mixed media using api.fxtwitter.com (bypasses 18+/NSFW auth)."""
    m = re.search(r'(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/(\d+)', url)
    if not m:
        raise ValueError("URL de X (Twitter) no válida.")
    user, twid = m.group(1), m.group(2)

    data = None
    last_err = None
    endpoints = [
        f"https://api.fxtwitter.com/2/status/{twid}",
        f"https://api.fxtwitter.com/{user}/status/{twid}",
    ]
    for api_url in endpoints:
        try:
            req = urllib.request.Request(
                api_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data and (data.get('status') or data.get('tweet')):
                    break
        except Exception as e:
            last_err = e

    if not data:
        raise ValueError(f"No se pudo consultar la API de X (Twitter): {last_err or 'Error de conexión'}")

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

    if not media_all:
        raise ValueError("Este Tweet no contiene videos ni imágenes descargables.")

    # If user specifically asked for MP3 audio
    if format_type == 'mp3':
        video_items = [item for item in media_all if item.get('type') == 'video']
        if not video_items:
            raise ValueError("Esta publicación de X solo contiene imágenes y no tiene audio.")
        vid_item = video_items[0]
        vid_url = vid_item.get('url')
        if not vid_url:
            raise ValueError("No se pudo obtener el enlace del video para extraer audio.")

        raw_vid = os.path.join(temp_subfolder, f"tw_{twid}_raw.mp4")
        urllib.request.urlretrieve(vid_url, raw_vid)
        mp3_file = os.path.join(temp_subfolder, f"tw_{twid}.mp3")
        cmd = ['ffmpeg', '-y', '-i', raw_vid, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else raw_vid

        thumb_path = None
        if vid_item.get('thumbnail_url'):
            raw_thumb = os.path.join(temp_subfolder, f"tw_thumb_{twid}.jpg")
            try:
                urllib.request.urlretrieve(vid_item['thumbnail_url'], raw_thumb)
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, final_file, temp_subfolder)
            except Exception:
                pass

        duration = vid_item.get('duration') or 0
        return {
            'type': 'audio',
            'file_path': final_file,
            'thumbnail_path': thumb_path,
            'title': text,
            'artist': author,
            'duration': int(duration) if duration else None,
            'width': None,
            'height': None,
            'filesize': os.path.getsize(final_file),
            'platform': 'X (Twitter)',
            'platform_emoji': '🐦',
            'is_audio': True,
            'format': 'mp3',
        }

    # Normal video or photo download (format_type == 'mp4')
    downloaded_items = []
    for idx, item in enumerate(media_all[:10]):
        itype = item.get('type')
        iurl = item.get('url')
        if not iurl:
            continue
        if itype == 'video':
            out_path = os.path.join(temp_subfolder, f"tw_{twid}_{idx}.mp4")
            urllib.request.urlretrieve(iurl, out_path)
            thumb_path = None
            if item.get('thumbnail_url'):
                raw_thumb = os.path.join(temp_subfolder, f"tw_thumb_{idx}.jpg")
                try:
                    urllib.request.urlretrieve(item['thumbnail_url'], raw_thumb)
                    thumb_path = convert_thumbnail_to_jpg(raw_thumb, out_path, temp_subfolder)
                except Exception:
                    pass
            if not thumb_path:
                thumb_path = convert_thumbnail_to_jpg(None, out_path, temp_subfolder)

            file_size = os.path.getsize(out_path)
            duration = item.get('duration') or 0
            if file_size > MAX_TELEGRAM_SIZE_BYTES and duration and 0 < duration < 1800:
                compressed = os.path.join(temp_subfolder, f"comp_{idx}.mp4")
                if compress_video_ffmpeg(out_path, compressed, float(duration)):
                    out_path = compressed
                    file_size = os.path.getsize(out_path)

            downloaded_items.append({
                'type': 'video',
                'file_path': out_path,
                'thumbnail_path': thumb_path,
                'duration': int(duration) if duration else None,
                'filesize': file_size,
            })
        else:
            # Photo
            out_path = os.path.join(temp_subfolder, f"tw_{twid}_{idx}.jpg")
            urllib.request.urlretrieve(iurl, out_path)
            downloaded_items.append({
                'type': 'photo',
                'file_path': out_path,
                'filesize': os.path.getsize(out_path),
            })

    if not downloaded_items:
        raise ValueError("No se pudo descargar la multimedia de este Tweet.")

    if len(downloaded_items) == 1:
        first = downloaded_items[0]
        return {
            'type': first['type'],
            'file_path': first['file_path'],
            'thumbnail_path': first.get('thumbnail_path'),
            'title': text,
            'artist': author,
            'duration': first.get('duration'),
            'width': None,
            'height': None,
            'filesize': first['filesize'],
            'platform': 'X (Twitter)',
            'platform_emoji': '🐦',
            'is_audio': False,
            'format': 'mp4' if first['type'] == 'video' else 'jpg',
        }

    return {
        'type': 'album',
        'media_items': downloaded_items,
        'title': text,
        'artist': author,
        'duration': None,
        'filesize': sum(m['filesize'] for m in downloaded_items),
        'platform': 'X (Twitter)',
        'platform_emoji': '🐦',
        'is_audio': False,
        'format': 'album',
    }


def download_instagram_direct(
    url: str,
    temp_subfolder: str,
    cookies_file: Optional[str] = None,
    format_type: str = "mp4"
) -> Dict[str, Any]:
    """Downloads Instagram video, photo, or carousel directly via mobile API (supports +18/age-restricted and photos)."""
    from yt_dlp.utils import traverse_obj
    ydl = yt_dlp.YoutubeDL({'cookiefile': cookies_file, 'quiet': True})
    ie = ydl.get_info_extractor('Instagram')
    ie.initialize()
    video_id, _ = ie._match_valid_url(url).group('id', 'url')
    media_id = str(yt_dlp.extractor.instagram._id_to_pk(video_id))
    api_url = f'{ie._API_BASE_URL}/media/{media_id}/info/'
    data = None
    try:
        data = ie._download_json(api_url, video_id, headers=ie._api_headers, impersonate=True)
    except Exception as e_info:
        logger.warning(f"Instagram mobile API info falló ({e_info}), intentando consulta GraphQL...")
        try:
            gql_res = ie._download_json(
                'https://www.instagram.com/api/graphql', video_id,
                fatal=False, impersonate=True,
                headers={
                    **ie._api_headers,
                    'X-FB-Friendly-Name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
                    'X-FB-LSD': getattr(ie, '_lsd_token', None),
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': f'https://www.instagram.com/p/{video_id}/',
                },
                data=yt_dlp.utils.urlencode_postdata({
                    'lsd': getattr(ie, '_lsd_token', None),
                    'fb_api_caller_class': 'RelayModern',
                    'fb_api_req_friendly_name': 'PolarisLoggedOutDesktopWWWPostRootContentQuery',
                    'server_timestamps': 'true',
                    'variables': json.dumps({'media_id': media_id}, separators=(',', ':')),
                    'doc_id': '27130156389949648',
                })
            )
            if gql_res:
                product = traverse_obj(gql_res, ('data', 'xig_polaris_media', 'if_not_gated_logged_out'))
                if product:
                    data = {'items': [product]}
        except Exception:
            pass

    if not data or not data.get('items'):
        raise ValueError(
            "🔒 Contenido con Restricción de Edad o Audiencia en Instagram.\n\n"
            "Instagram bloquea el acceso anónimo a esta publicación (contenido clasificado como videojuegos, edad mínima de cuenta o audiencia restringida).\n\n"
            "💡 Para descargar videos o fotos con restricción: Añade tu sesión de Instagram en tu archivo .env o en las variables de entorno de tu hosting:\n"
            "INSTAGRAM_SESSIONID=tu_session_id\n\n"
            "(Obtén el valor de la cookie 'sessionid' desde instagram.com en tu navegador -> F12 -> Almacenamiento/Storage -> Cookies)"
        )

    item = data['items'][0]
    caption = traverse_obj(item, ('caption', 'text')) or "Publicación de Instagram"
    author = traverse_obj(item, ('user', 'username')) or "Instagram"
    media_type = item.get('media_type')  # 1: Photo, 2: Video, 8: Carousel

    # If user requested MP3 audio
    if format_type == 'mp3':
        vid_url = None
        if media_type == 2:
            video_versions = item.get('video_versions') or []
            if video_versions:
                vid_url = video_versions[0].get('url')
        elif media_type == 8:
            carousel = item.get('carousel_media') or []
            for sub in carousel:
                if sub.get('media_type') == 2:
                    vids = sub.get('video_versions') or []
                    if vids:
                        vid_url = vids[0].get('url')
                        break

        if not vid_url:
            raise ValueError("Esta publicación de Instagram solo contiene fotos y no tiene audio.")

        raw_vid = os.path.join(temp_subfolder, f"ig_{video_id}_raw.mp4")
        urllib.request.urlretrieve(vid_url, raw_vid)
        mp3_file = os.path.join(temp_subfolder, f"ig_{video_id}.mp3")
        cmd = ['ffmpeg', '-y', '-i', raw_vid, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_file]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_file = mp3_file if (os.path.exists(mp3_file) and os.path.getsize(mp3_file) > 0) else raw_vid

        candidates = traverse_obj(item, ('image_versions2', 'candidates')) or []
        thumb_path = None
        if candidates:
            raw_thumb = os.path.join(temp_subfolder, "ig_thumb.jpg")
            try:
                urllib.request.urlretrieve(candidates[0]['url'], raw_thumb)
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, final_file, temp_subfolder)
            except Exception:
                pass

        duration = item.get('video_duration', 0) or 0
        return {
            'type': 'audio',
            'file_path': final_file,
            'thumbnail_path': thumb_path,
            'title': caption,
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

    # 1. Single photo
    if media_type == 1:
        candidates = traverse_obj(item, ('image_versions2', 'candidates')) or []
        if not candidates:
            raise ValueError("No se encontró la imagen de esta publicación de Instagram.")
        img_url = candidates[0]['url']
        out_photo = os.path.join(temp_subfolder, f"ig_{video_id}.jpg")
        urllib.request.urlretrieve(img_url, out_photo)
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

    # 2. Single video
    if media_type == 2:
        video_versions = item.get('video_versions') or []
        if not video_versions:
            raise ValueError("No se encontró el video de este Reel/Post de Instagram.")
        vid_url = video_versions[0]['url']
        out_video = os.path.join(temp_subfolder, f"ig_{video_id}.mp4")
        urllib.request.urlretrieve(vid_url, out_video)
        duration = item.get('video_duration', 0) or 0
        file_size = os.path.getsize(out_video)

        if file_size > MAX_TELEGRAM_SIZE_BYTES and duration and 0 < duration < 1800:
            compressed = os.path.join(temp_subfolder, f"comp_ig_{video_id}.mp4")
            if compress_video_ffmpeg(out_video, compressed, float(duration)):
                out_video = compressed
                file_size = os.path.getsize(out_video)

        candidates = traverse_obj(item, ('image_versions2', 'candidates')) or []
        thumb_path = None
        if candidates:
            raw_thumb = os.path.join(temp_subfolder, "ig_thumb.jpg")
            try:
                urllib.request.urlretrieve(candidates[0]['url'], raw_thumb)
                thumb_path = convert_thumbnail_to_jpg(raw_thumb, out_video, temp_subfolder)
            except Exception:
                pass
        if not thumb_path:
            thumb_path = convert_thumbnail_to_jpg(None, out_video, temp_subfolder)

        return {
            'type': 'video',
            'file_path': out_video,
            'thumbnail_path': thumb_path,
            'title': caption,
            'artist': author,
            'duration': int(duration) if duration else None,
            'width': video_versions[0].get('width'),
            'height': video_versions[0].get('height'),
            'filesize': file_size,
            'platform': 'Instagram',
            'platform_emoji': '📸',
            'is_audio': False,
            'format': 'mp4',
        }

    # 3. Carousel (Album)
    if media_type == 8:
        carousel = item.get('carousel_media') or []
        downloaded = []
        for idx, sub in enumerate(carousel[:10]):
            sub_type = sub.get('media_type')
            if sub_type == 1:
                cands = traverse_obj(sub, ('image_versions2', 'candidates')) or []
                if cands:
                    p_path = os.path.join(temp_subfolder, f"ig_slide_{idx}.jpg")
                    urllib.request.urlretrieve(cands[0]['url'], p_path)
                    downloaded.append({
                        'type': 'photo',
                        'file_path': p_path,
                        'filesize': os.path.getsize(p_path)
                    })
            elif sub_type == 2:
                vids = sub.get('video_versions') or []
                if vids:
                    v_path = os.path.join(temp_subfolder, f"ig_slide_{idx}.mp4")
                    urllib.request.urlretrieve(vids[0]['url'], v_path)
                    dur = sub.get('video_duration', 0) or 0
                    fsize = os.path.getsize(v_path)
                    if fsize > MAX_TELEGRAM_SIZE_BYTES and dur and 0 < dur < 1800:
                        comp = os.path.join(temp_subfolder, f"comp_slide_{idx}.mp4")
                        if compress_video_ffmpeg(v_path, comp, float(dur)):
                            v_path = comp
                            fsize = os.path.getsize(v_path)
                    t_path = None
                    cands = traverse_obj(sub, ('image_versions2', 'candidates')) or []
                    if cands:
                        raw_t = os.path.join(temp_subfolder, f"ig_thumb_{idx}.jpg")
                        try:
                            urllib.request.urlretrieve(cands[0]['url'], raw_t)
                            t_path = convert_thumbnail_to_jpg(raw_t, v_path, temp_subfolder)
                        except Exception:
                            pass
                    downloaded.append({
                        'type': 'video',
                        'file_path': v_path,
                        'thumbnail_path': t_path,
                        'duration': int(dur) if dur else None,
                        'filesize': fsize
                    })

        if downloaded:
            if len(downloaded) == 1:
                first = downloaded[0]
                return {
                    'type': first['type'],
                    'file_path': first['file_path'],
                    'thumbnail_path': first.get('thumbnail_path'),
                    'title': caption,
                    'artist': author,
                    'duration': first.get('duration'),
                    'filesize': first['filesize'],
                    'platform': 'Instagram',
                    'platform_emoji': '📸',
                    'is_audio': False,
                    'format': 'mp4' if first['type'] == 'video' else 'jpg',
                }
            return {
                'type': 'album',
                'media_items': downloaded,
                'title': caption,
                'artist': author,
                'filesize': sum(m['filesize'] for m in downloaded),
                'platform': 'Instagram',
                'platform_emoji': '📸',
                'is_audio': False,
                'format': 'album',
            }

    raise ValueError("Formato de contenido de Instagram no reconocido.")


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

        # 3. Strip tracking and share parameters (?igsh=..., ?stkn=..., utm_*, etc.)
        qs = parse_qs(parsed.query)
        filtered_qs = {
            k: v for k, v in qs.items()
            if not (k.startswith('utm_') or k in ['igsh', 'ig_mid', 'src', 'fbclid', 'ig_rid', 'stkn'])
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
    Sets up cookies from environment variables if present, merges them with existing file,
    or automatically exports relevant cookies from installed local browsers (Firefox, Chrome, etc.).
    Supports COOKIES_CONTENT, YOUTUBE_COOKIES, INSTAGRAM_COOKIES, and INSTAGRAM_SESSIONID.
    """
    ig_sessionid = (
        os.getenv("INSTAGRAM_SESSIONID")
        or os.getenv("INSTAGRAM_SESSION_ID")
        or os.getenv("IG_SESSIONID")
        or os.getenv("IG_SESSION_ID")
        or os.getenv("INSTAGRAM_SESSION")
        or os.getenv("IG_SESSION")
        or os.getenv("SESSIONID")
        or os.getenv("sessionid")
        or os.getenv("instagram_sessionid")
        or os.getenv("instagram_session_id")
        or os.getenv("ig_sessionid")
    )
    env_sources = [
        os.getenv("COOKIES_CONTENT"),
        os.getenv("YOUTUBE_COOKIES"),
        os.getenv("INSTAGRAM_COOKIES"),
    ]

    proj_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        cookies_path if os.path.isabs(cookies_path) else os.path.join(proj_dir, cookies_path),
        os.path.join(tempfile.gettempdir(), os.path.basename(cookies_path)),
    ]

    existing_lines = []
    found_existing = False
    chosen_path = candidates[0]

    for cand in candidates:
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            try:
                with open(cand, "r", encoding="utf-8", errors="ignore") as f:
                    existing_lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                found_existing = True
                chosen_path = cand
                break
            except Exception:
                continue

    new_lines = []
    for src in env_sources:
        if src and src.strip():
            for line in src.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    new_lines.append(line)

    if ig_sessionid and ig_sessionid.strip():
        sid = ig_sessionid.strip().strip('"').strip("'").strip()
        if "sessionid=" in sid:
            sid = sid.split("sessionid=")[1].split(";")[0].strip()
        user_id = sid.split('%3A')[0].split(':')[0]
        if user_id.isdigit():
            new_lines.append(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tds_user_id\t{user_id}")
        new_lines.append(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{sid}")

    if new_lines:
        cookies_dict = {}
        for l in existing_lines + new_lines:
            parts = l.split('\t')
            if len(parts) >= 7:
                domain, name = parts[0], parts[5]
                cookies_dict[(domain, name)] = l
            else:
                cookies_dict[l] = l

        for cand in candidates:
            try:
                with open(cand, "w", encoding="utf-8") as f:
                    f.write("# Netscape HTTP Cookie File\n")
                    for c_line in cookies_dict.values():
                        f.write(c_line + "\n")
                return cand
            except Exception as e:
                logger.warning(f"Error escribiendo archivo de cookies en {cand}: {e}")

    if found_existing:
        return chosen_path

    # Try automatic extraction from local browsers
    for browser in ['firefox', 'chrome', 'brave', 'chromium', 'edge']:
        try:
            ydl_test = yt_dlp.YoutubeDL({'cookiesfrombrowser': (browser, None, None, None), 'quiet': True})
            if ydl_test.cookiejar and len(ydl_test.cookiejar) > 0:
                with open(cookies_path, "w", encoding="utf-8") as f:
                    f.write("# Netscape HTTP Cookie File\n")
                    for c in ydl_test.cookiejar:
                        if any(d in c.domain for d in ['youtube.com', 'instagram.com', 'tiktok.com', 'twitter.com', 'x.com', 'facebook.com', 'spotify.com']):
                            initial_dot = "TRUE" if c.domain.startswith(".") else "FALSE"
                            secure = "TRUE" if c.secure else "FALSE"
                            expires = str(c.expires) if c.expires else "0"
                            f.write(f"{c.domain}\t{initial_dot}\t{c.path}\t{secure}\t{expires}\t{c.name}\t{c.value}\n")
                if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
                    logger.info(f"Cookies exportadas exitosamente desde {browser} a {cookies_path}")
                    return cookies_path
        except Exception:
            continue

    return None


def get_js_runtimes_config() -> Optional[Dict[str, Any]]:
    """Detects available JS runtime (node/deno/bun) for yt-dlp challenge solving."""
    node_path = shutil.which("node") or shutil.which("nodejs")
    if not node_path:
        nvm_patterns = [
            os.path.expanduser("~/.nvm/versions/node/*/bin/node"),
            "/var/home/*/.nvm/versions/node/*/bin/node",
            "/home/*/.nvm/versions/node/*/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/node",
            "/usr/bin/nodejs",
            os.path.expanduser("~/.local/bin/node"),
        ]
        for pattern in nvm_patterns:
            matches = glob.glob(pattern)
            if matches:
                matches.sort()
                found = matches[-1]
                if os.path.isfile(found) and os.access(found, os.X_OK):
                    node_path = found
                    break

    if node_path:
        return {'node': {'path': node_path}}

    deno_path = shutil.which("deno")
    if not deno_path:
        for p in [
            os.path.expanduser("~/.deno/bin/deno"),
            "/var/home/*/.deno/bin/deno",
            "/home/*/.deno/bin/deno",
            "/usr/local/bin/deno",
            "/usr/bin/deno",
        ]:
            matches = glob.glob(p) if '*' in p else ([p] if os.path.exists(p) else [])
            for cand in matches:
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    deno_path = cand
                    break
            if deno_path:
                break
    if deno_path:
        return {'deno': {'path': deno_path}}

    bun_path = shutil.which("bun")
    if not bun_path:
        for p in [
            os.path.expanduser("~/.bun/bin/bun"),
            "/var/home/*/.bun/bin/bun",
            "/home/*/.bun/bin/bun",
            "/usr/local/bin/bun",
            "/usr/bin/bun",
        ]:
            matches = glob.glob(p) if '*' in p else ([p] if os.path.exists(p) else [])
            for cand in matches:
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    bun_path = cand
                    break
            if bun_path:
                break
    if bun_path:
        return {'bun': {'path': bun_path}}

    return None


class VideoDownloader:
    def __init__(self, temp_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        self.temp_dir = temp_dir or tempfile.gettempdir()
        self.cookies_path_setting = cookies_file or "cookies.txt"
        self.cookies_file = setup_cookies_file(self.cookies_path_setting)

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
                yt_opts.pop('cookiefile', None)
                js_cfg = get_js_runtimes_config()
                if js_cfg:
                    yt_opts['js_runtimes'] = js_cfg
                yt_opts['extractor_args'] = {'youtube': {'player_client': ['visionos']}}
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
        # Ensure fresh cookies from environment are loaded
        self.cookies_file = setup_cookies_file(getattr(self, 'cookies_path_setting', 'cookies.txt'))

        if is_spotify_url(url):
            return self._sync_download_spotify(url, output_template, temp_subfolder)

        format_type = format_type.lower().strip()
        if format_type not in ['mp3', 'mp4']:
            format_type = 'mp4'

        platform_name, platform_emoji = detect_platform(url)
        is_yt = is_youtube_url(url)

        if platform_name == "Instagram":
            url = normalize_instagram_url(url)
            has_session = False
            if self.cookies_file and os.path.exists(self.cookies_file):
                try:
                    with open(self.cookies_file, 'r', errors='ignore') as cf:
                        has_session = 'sessionid' in cf.read()
                except Exception:
                    pass
            logger.info(f"📸 Intento de descarga Instagram: cookies_file={self.cookies_file}, sessionid_activo={has_session}")

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

        # For non-YouTube and non-Instagram platforms, set standard User-Agent.
        # For YouTube and Instagram, DO NOT override User-Agent so yt-dlp matches internal client and TLS impersonation signatures.
        if not is_yt and platform_name != "Instagram":
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
                'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/'
                'bestvideo[height<=720]+bestaudio/'
                'best[height<=720][ext=mp4]/'
                'best[height<=720]/'
                'bestvideo+bestaudio/best'
            )
            ydl_opts['merge_output_format'] = 'mp4'

        if self.cookies_file:
            ydl_opts['cookiefile'] = self.cookies_file

        # 1. TikTok special handling: try yt-dlp first; if blocked or photos, fallback to TikWM API directly
        if platform_name == "TikTok":
            try:
                active_ydl = yt_dlp.YoutubeDL(ydl_opts)
                info = active_ydl.extract_info(url, download=True)
            except Exception as e_tt:
                logger.warning(f"yt-dlp falló para TikTok ({e_tt}), ejecutando descarga via TikWM API...")
                try:
                    return download_tiktok_via_api(url, format_type, temp_subfolder)
                except Exception as e_fallback:
                    logger.error(f"TikWM fallback también falló: {e_fallback}")
                    raise ValueError(f"No se pudo descargar el contenido de TikTok: {e_tt}")

        # 2. X (Twitter) special handling: try yt-dlp first; if blocked (+18/NSFW) or photos, fallback to FxTwitter
        elif platform_name in ("X (Twitter)", "Twitter"):
            try:
                active_ydl = yt_dlp.YoutubeDL(ydl_opts)
                info = active_ydl.extract_info(url, download=True)
            except Exception as e_tw:
                logger.warning(f"yt-dlp falló para X/Twitter ({e_tw}), ejecutando descarga via FxTwitter API fallback...")
                try:
                    return download_twitter_fallback(url, temp_subfolder, format_type=format_type)
                except Exception as e_tw_fallback:
                    logger.error(f"FxTwitter fallback también falló: {e_tw_fallback}")
                    raise ValueError(f"No se pudo descargar el contenido de X (Twitter): {e_tw_fallback}")

        # 3. Instagram special handling: try yt-dlp first; if blocked (+18) or photos, fallback to Instagram API direct
        elif platform_name == "Instagram":
            try:
                active_ydl = yt_dlp.YoutubeDL(ydl_opts)
                info = active_ydl.extract_info(url, download=True)
            except Exception as e_ig:
                logger.warning(f"yt-dlp falló para Instagram ({e_ig}), intentando descarga directa...")
                try:
                    return download_instagram_direct(url, temp_subfolder, cookies_file=self.cookies_file, format_type=format_type)
                except Exception as e_ig_fallback:
                    logger.error(f"Instagram direct download también falló: {e_ig_fallback}")
                    err_lower = str(e_ig).lower()
                    if any(k in err_lower for k in [
                        'ciertas audiencias', 'audiences', 'restricted', 'restricción',
                        'empty media response', 'login', 'checkpoint', 'disponible para todo el mundo'
                    ]):
                        raise ValueError(
                            "🔒 Contenido con Restricción de Edad o Audiencia en Instagram.\n\n"
                            "Instagram bloquea el acceso anónimo a esta publicación (contenido clasificado como videojuegos, edad mínima de cuenta o audiencia restringida).\n\n"
                            "💡 Para descargar videos o fotos con restricción: Añade tu sesión de Instagram en tu archivo .env o en las variables de entorno de tu hosting:\n"
                            "INSTAGRAM_SESSIONID=tu_session_id\n\n"
                            "(Obtén el valor de la cookie 'sessionid' desde instagram.com en tu navegador -> F12 -> Almacenamiento/Storage -> Cookies)"
                        )
                    raise ValueError(f"No se pudo descargar el contenido de Instagram: {e_ig_fallback}")

        else:
            # Execution with sequential client and cookie fallbacks for YouTube
            # Note: Certain clients like 'android', 'ios' do NOT support cookies in yt-dlp and will be skipped
            # if cookiefile is present. Furthermore, if cookies are expired, flagged, or in SABR experiment,
            # YouTube responds with "The page needs to be reloaded" or returns 0 formats.
            # Therefore, we try with cookies (if available), then clean attempts without cookies, and across clients.
            if is_yt:
                has_cookies = bool(self.cookies_file and os.path.exists(self.cookies_file))
                attempts = []
                # 1. VisionOS client (Bypasses YouTube datacenter/cloud IP bot blocks on Render/VPS without cookies)
                attempts.append(("visionos (sin cookies)", ['visionos'], False))
                # 2. Android VR client (Resilient fallback for datacenter IPs)
                attempts.append(("android_vr (sin cookies)", ['android_vr'], False))
                # 3. If cookies are provided, try with cookies (for age-restricted or private videos)
                if has_cookies:
                    attempts.append(("default (con cookies)", None, True))
                # 4. Clean default client WITHOUT cookies
                attempts.append(("default (sin cookies)", None, False))
                # 5. Android client WITHOUT cookies
                attempts.append(("android (sin cookies)", ['android'], False))
                # 6. Web client WITHOUT cookies
                attempts.append(("web (sin cookies)", ['web'], False))
                # 7. Web client with cookies (if cookies exist)
                if has_cookies:
                    attempts.append(("web (con cookies)", ['web'], True))
                # 8. iOS client WITHOUT cookies
                attempts.append(("ios (sin cookies)", ['ios'], False))
            else:
                attempts = [("default", None, bool(self.cookies_file))]

            info = None
            active_ydl = None
            last_error = None

            for attempt_name, client, use_cookies in attempts:
                attempt_opts = dict(ydl_opts)
                if not use_cookies:
                    attempt_opts.pop('cookiefile', None)
                if client:
                    attempt_opts['extractor_args'] = {'youtube': {'player_client': client}}
                    # Ensure clients that do not support cookies never receive a cookiefile
                    if any(c in ('android', 'ios', 'visionos', 'android_vr') for c in client):
                        attempt_opts.pop('cookiefile', None)

                try:
                    active_ydl = yt_dlp.YoutubeDL(attempt_opts)
                    info = active_ydl.extract_info(url, download=True)
                    if info:
                        logger.info(f"✅ Descarga exitosa de YouTube usando estrategia: {attempt_name}")
                        break
                except Exception as e:
                    last_error = e
                    err_msg = str(e)
                    logger.warning(f"Download attempt '{attempt_name}' failed: {err_msg}")
                    if 'Private video' in err_msg or 'This video is private' in err_msg:
                        raise ValueError("El video es privado o no está disponible.")
                    elif 'Video unavailable' in err_msg:
                        raise ValueError("El video no está disponible o fue eliminado.")
                    continue

        if not info:
            err_text = str(last_error or "Error desconocido")
            err_lower = err_text.lower()
            if any(k in err_lower for k in ['sign in to confirm your age', 'inappropriate for some users', 'age-restricted', 'restricción de edad']):
                raise ValueError(
                    "🔒 Este video de YouTube tiene restricción de edad (+18) o requiere inicio de sesión.\n\n"
                    "💡 Para descargarlo: Envía tu archivo cookies.txt al bot o usa el comando /set_yt."
                )
            elif 'sign in to confirm you’re not a bot' in err_lower or 'bot' in err_lower or 'failed to extract any player response' in err_lower:
                raise ValueError(
                    "YouTube ha bloqueado temporalmente las descargas para la IP del servidor en la nube (Render). "
                    "Para solucionarlo de inmediato, añade tus cookies en la variable de entorno YOUTUBE_COOKIES en Render o usa /set_yt."
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
            if platform_name in ("X (Twitter)", "Twitter"):
                try:
                    return download_twitter_fallback(url, temp_subfolder, format_type=format_type)
                except Exception:
                    pass
            elif platform_name == "Instagram":
                try:
                    return download_instagram_direct(url, temp_subfolder, cookies_file=self.cookies_file, format_type=format_type)
                except Exception:
                    pass
            elif platform_name == "TikTok":
                try:
                    return download_tiktok_via_api(url, format_type, temp_subfolder)
                except Exception:
                    pass
            raise FileNotFoundError(f"No se encontró el archivo descargado.")

        file_size = os.path.getsize(result_file)

        # Compress video if slightly over 50MB
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
