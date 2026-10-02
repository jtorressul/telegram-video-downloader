"""Spotify: public embed metadata (no API key) + matching audio from SoundCloud / YouTube.

Spotify's own audio is DRM protected and is never touched; only titles, artists,
durations and cover art are read from the public embed page.
"""
import json
import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import yt_dlp

import config
from .base import Strategy
from .common import apply_id3_tags, convert_thumbnail_to_jpg, ydl_download
from .errors import NotFound, Unknown, Unsupported, classify
from .net import DIRECT, Http, Route, ydl_route_opts

logger = logging.getLogger(__name__)
PLATFORM, EMOJI = "Spotify", "🟢"
_URL_RE = re.compile(r'open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]+)')
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_BAD_WORDS = ('remix', 'cover', 'live', 'karaoke', 'instrumental', 'sped up', 'slowed', '8d', 'nightcore',
              'reverb', 'acoustic', 'lyrics video', 'reaction')
DURATION_TOLERANCE_S = 7


def parse_spotify_url(url: str) -> tuple:
    if 'spotify.link' in url or 'spotify.app.link' in url:
        with Http(DIRECT, timeout=12) as http:
            url = http.get(url, check=False).url
    m = _URL_RE.search(url)
    if not m:
        raise Unsupported("Solo se admiten enlaces de canciones, álbumes y playlists de Spotify.")
    return m.group(1), m.group(2)


def fetch_entity(kind: str, spotify_id: str) -> Dict[str, Any]:
    with Http(DIRECT, timeout=15) as http:
        resp = http.get(f"https://open.spotify.com/embed/{kind}/{spotify_id}", check=False)
    if resp.status_code == 404:
        raise NotFound("Spotify embed 404")
    return parse_embed_page(resp.text)


def parse_embed_page(page: str) -> Dict[str, Any]:
    """Returns {'kind', 'name', 'cover', 'tracks': [{title, artist, duration}]}."""
    m = _NEXT_RE.search(page)
    if not m:
        raise Unknown("Spotify embed sin __NEXT_DATA__")
    data = json.loads(m.group(1))
    ent = (((data.get('props') or {}).get('pageProps') or {}).get('state') or {}).get('data', {}).get('entity')
    if not ent:
        raise NotFound("Spotify: entidad no encontrada")
    cover = _cover(ent)
    if ent.get('type') == 'track':
        artists = ", ".join(a.get('name', '') for a in ent.get('artists') or [] if a.get('name'))
        tracks = [{'title': ent.get('name') or ent.get('title'), 'artist': artists,
                   'duration': (ent.get('duration') or 0) / 1000, 'album': None}]
    else:
        tracks = [{'title': t.get('title'), 'artist': t.get('subtitle') or '',
                   'duration': (t.get('duration') or 0) / 1000,
                   'album': ent.get('name') if ent.get('type') == 'album' else None}
                  for t in ent.get('trackList') or [] if t.get('title')]
    return {'kind': ent.get('type'), 'name': ent.get('name') or ent.get('title') or 'Spotify',
            'subtitle': ent.get('subtitle') or '', 'cover': cover, 'tracks': tracks}


def _cover(ent: Dict[str, Any]) -> Optional[str]:
    sources = ((ent.get('coverArt') or {}).get('sources')) or ((ent.get('visualIdentity') or {}).get('image')) or []
    if not sources:
        return None
    best = max(sources, key=lambda s: s.get('width') or s.get('maxWidth') or 0)
    return best.get('url')


# ------------------------------------------------------------------------------
# Matching
# ------------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9 ]+', ' ', s)


def score_candidate(track: Dict[str, Any], cand_title: str, cand_uploader: str,
                    cand_duration: Optional[float]) -> float:
    """Higher is better; -1 means reject. Duration is the strongest signal (and filters 30 s previews)."""
    want = track.get('duration') or 0
    if want and cand_duration:
        diff = abs(want - cand_duration)
        if diff > DURATION_TOLERANCE_S:
            return -1
        score = 50 - diff * 4
    else:
        score = 10
    title_n, cand_n = _norm(track['title']), _norm(f"{cand_title} {cand_uploader}")
    title_words = set(title_n.split())
    if title_words:
        score += 30 * len(title_words & set(cand_n.split())) / len(title_words)
    artist_words = set(_norm(track.get('artist', '')).split())
    if artist_words:
        score += 20 * len(artist_words & set(cand_n.split())) / len(artist_words)
    for w in _BAD_WORDS:
        if w in cand_n and w not in title_n:
            score -= 25
    return score


def rank_matches(track: Dict[str, Any], route: Route) -> List[str]:
    """Candidate URLs, best first, from SoundCloud and YouTube searches."""
    query = f"{track.get('artist', '')} {track['title']}".strip()
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'extract_flat': 'in_playlist'}
    opts.update(ydl_route_opts(route))
    scored = []
    # SoundCloud first: it tolerates datacenter IPs far better than YouTube
    for search in (f"scsearch5:{query}", f"ytsearch5:{query} audio"):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                res = ydl.extract_info(search, download=False) or {}
        except Exception as e:
            logger.info(f"Búsqueda '{search}' falló: {e}")
            continue
        for e in res.get('entries') or []:
            url = e and (e.get('url') or e.get('webpage_url'))
            if not url:
                continue
            s = score_candidate(track, e.get('title') or '', e.get('uploader') or e.get('channel') or '',
                                e.get('duration'))
            if s > 0:
                scored.append((s, url))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [u for _, u in scored]


def _download_track(track: Dict[str, Any], idx: int, workdir: str, route: Route,
                    cover_path: Optional[str]) -> Dict[str, Any]:
    candidates = rank_matches(track, route)
    if not candidates:
        raise NotFound(f"No se encontró audio para '{track.get('artist')} - {track['title']}'")
    last: Exception = NotFound("sin candidatos")
    # Some SoundCloud uploads are DRM protected or blocked: fall through to the next candidate
    for n, url in enumerate(candidates[:4]):
        sub = os.path.join(workdir, f"t{idx:02d}_{n}")
        os.makedirs(sub, exist_ok=True)
        try:
            res = ydl_download(url, 'mp3', sub, route, PLATFORM, EMOJI, {'writethumbnail': False})
            break
        except Exception as e:
            last = e
            logger.info(f"Candidato {url} falló: {str(e)[:120]}")
    else:
        raise last
    apply_id3_tags(res['file_path'], track['title'], track.get('artist') or 'Spotify', cover_path,
                   album=track.get('album'))
    res.update(title=track['title'], artist=track.get('artist') or 'Spotify',
               thumbnail_path=cover_path, platform=PLATFORM, platform_emoji=EMOJI,
               filesize=os.path.getsize(res['file_path']))
    return res


def _spotify(url: str, format_type: str, workdir: str, route: Route) -> Dict[str, Any]:
    kind, sid = parse_spotify_url(url)
    ent = fetch_entity(kind, sid)
    tracks = ent['tracks'][:config.SPOTIFY_MAX_TRACKS]
    if not tracks:
        raise NotFound("La lista de Spotify está vacía.")

    cover_path = None
    if ent.get('cover'):
        try:
            with Http(DIRECT, timeout=15) as http:
                raw = http.download(ent['cover'], os.path.join(workdir, "spotify_cover.jpg"))
            cover_path = convert_thumbnail_to_jpg(raw, "", workdir)
        except Exception:
            cover_path = None

    if len(tracks) == 1:
        return _download_track(tracks[0], 0, workdir, route, cover_path)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_download_track, t, i, workdir, route, cover_path) for i, t in enumerate(tracks)]
        items, errors = [], []
        for f in futures:
            try:
                items.append(f.result())
            except Exception as e:
                errors.append(classify(e))
    if not items:
        raise max(errors, key=lambda e: e.priority) if errors else Unknown("sin pistas")
    title = ent['name'] + (f" — {ent['subtitle']}" if ent.get('subtitle') else "")
    if len(errors):
        title += f" ({len(items)}/{len(tracks)} pistas)"
    return {
        'type': 'carousel',
        'media_items': items,
        'title': title,
        'artist': ent.get('subtitle') or 'Spotify',
        'duration': None,
        'filesize': sum(i['filesize'] for i in items),
        'platform': PLATFORM,
        'platform_emoji': EMOJI,
        'is_audio': True,
        'format': 'mp3',
    }


STRATEGIES = [Strategy("spotify-match", _spotify)]
