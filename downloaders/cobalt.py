"""Cobalt (v10+ API) bridge: a third-party server fetches the media, so our IP reputation doesn't matter."""
import json
import logging
import os
from typing import Any, Dict

import config
from .common import download_media_list, ensure_size, convert_thumbnail_to_jpg, media_result, to_mp3
from .errors import NotFound, Private, RegionOrAgeLocked, Unknown, classify
from .net import Http

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(config.COBALT_INSTANCES)


def _request(instance: str, url: str, format_type: str) -> Dict[str, Any]:
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if config.COBALT_API_KEY:
        headers['Authorization'] = f"Api-Key {config.COBALT_API_KEY}"
    payload = {
        "url": url,
        "downloadMode": "audio" if format_type == "mp3" else "auto",
        "audioFormat": "mp3" if format_type == "mp3" else "best",
        "videoQuality": "720",
    }
    with Http(timeout=25) as http:
        resp = http.post(instance, data=json.dumps(payload), headers=headers, check=False)
        return resp.json()


def download(url: str, format_type: str, workdir: str, platform: str, emoji: str) -> Dict[str, Any]:
    last: Exception = Unknown("Cobalt sin instancias")
    for instance in config.COBALT_INSTANCES:
        try:
            data = _request(instance, url, format_type)
        except Exception as e:
            last = classify(e)
            continue
        status = data.get('status')
        if status == 'error':
            code = str((data.get('error') or {}).get('code', ''))
            if 'private' in code:
                raise Private(code)
            if 'age' in code or 'region' in code:
                last = RegionOrAgeLocked(code)
            elif 'unavailable' in code or 'not_found' in code:
                last = NotFound(code)
            else:
                last = Unknown(f"cobalt {instance}: {code}")
            continue
        with Http(timeout=60) as http:
            if status in ('redirect', 'tunnel') and data.get('url'):
                ext = 'mp3' if format_type == 'mp3' else 'mp4'
                raw = http.download(data['url'], os.path.join(workdir, f"cobalt_raw.{ext}"))
                if format_type == 'mp3':
                    final = to_mp3(raw, os.path.join(workdir, "cobalt.mp3"))
                    return media_result('audio', final, platform, emoji, f"Audio de {platform}", platform)
                filename = (data.get('filename') or '').lower()
                if filename.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    return media_result('photo', raw, platform, emoji, f"Foto de {platform}", platform)
                res = media_result('video', raw, platform, emoji, f"Video de {platform}", platform,
                                   convert_thumbnail_to_jpg(None, raw, workdir))
                ensure_size(res)
                return res
            if status == 'picker' and data.get('picker'):
                media = [{'type': p.get('type') or 'photo', 'url': p.get('url'),
                          'thumbnail_url': p.get('thumb')} for p in data['picker']]
                return download_media_list(http, media, format_type, workdir, 'cobalt', platform, emoji,
                                           f"Publicación de {platform}", platform)
        last = Unknown(f"cobalt {instance}: respuesta inesperada {str(data)[:120]}")
    raise last
