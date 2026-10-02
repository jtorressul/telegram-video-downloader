import ipaddress
import os

import pytest

import config
from downloaders import base, health
from downloaders.base import Strategy, run_chain
from downloaders.common import detect_platform, extract_youtube_id
from downloaders.errors import (IPBlocked, NotFound, Private, RateLimited, RegionOrAgeLocked, Unknown,
                                classify)
from downloaders.instagram import (extract_instagram_shortcode, media_from_igexport, normalize_instagram_url,
                                  parse_embed_html)
from downloaders.net import DIRECT, Route, random_ipv6
from downloaders.spotify import parse_embed_page, score_candidate
from downloaders.twitter import parse_tweet_url, syndication_token

HERE = os.path.dirname(__file__)


@pytest.fixture(autouse=True)
def reset_health(monkeypatch):
    health._breakers.clear()
    health._stats.clear()
    monkeypatch.setattr(health, "throttle", lambda platform: None)


# --- URL parsing ---------------------------------------------------------------
@pytest.mark.parametrize("url,platform", [
    ("https://www.tiktok.com/@a/video/1", "TikTok"),
    ("https://vm.tiktok.com/ZMabc/", "TikTok"),
    ("https://x.com/a/status/1", "X (Twitter)"),
    ("https://twitter.com/a/status/1", "X (Twitter)"),
    ("https://www.instagram.com/reel/abc/", "Instagram"),
    ("https://youtu.be/jNQXAC9IVRw", "YouTube"),
    ("https://music.youtube.com/watch?v=jNQXAC9IVRw", "YouTube Music"),
    ("https://open.spotify.com/track/x", "Spotify"),
    ("https://example.com/box.mp4", "Web Video"),
])
def test_detect_platform(url, platform):
    assert detect_platform(url)[0] == platform


def test_detect_platform_does_not_match_x_inside_other_domains():
    assert detect_platform("https://www.dropbox.com/s/video.mp4")[0] == "Web Video"


def test_youtube_ids():
    assert extract_youtube_id("https://www.youtube.com/shorts/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://youtu.be/jNQXAC9IVRw?t=3") == "jNQXAC9IVRw"


def test_instagram_urls():
    assert extract_instagram_shortcode("https://www.instagram.com/someuser/reel/ABC_123/?igsh=x") == "ABC_123"
    assert normalize_instagram_url("https://instagram.com/reels/XYZ/") == "https://www.instagram.com/reel/XYZ/"
    assert normalize_instagram_url("https://www.instagram.com/p/XYZ/?img_index=2") == "https://www.instagram.com/p/XYZ/"


def test_tweet_url_and_token():
    assert parse_tweet_url("https://x.com/NASA/status/123?s=20") == ("NASA", "123")
    assert parse_tweet_url("https://twitter.com/i/web/status/456") == ("i", "456")
    token = syndication_token("1585341984679469056")
    assert token and "." not in token and "0" not in token


# --- Error classification --------------------------------------------------------
@pytest.mark.parametrize("text,cls", [
    ("ERROR: [TikTok] 768: Video not available, status code 10231", RegionOrAgeLocked),
    ("Sign in to confirm your age", RegionOrAgeLocked),
    ("Sign in to confirm you’re not a bot", IPBlocked),
    ("HTTP Error 403: Forbidden", IPBlocked),
    ("HTTP Error 429: Too Many Requests", RateLimited),
    ("Private video", Private),
    ("Video unavailable", NotFound),
    ("something weird", Unknown),
])
def test_classify(text, cls):
    assert isinstance(classify(Exception(text)), cls)


def test_typed_errors_are_value_errors_with_user_message():
    err = IPBlocked("raw detail")
    assert isinstance(err, ValueError)
    assert "bloqueando" in str(err) and err.detail == "raw detail"


# --- Strategy chain ----------------------------------------------------------------
def _ok(url, fmt, workdir, route):
    return {"type": "video", "route": route.name}


def _fail(exc):
    def run(url, fmt, workdir, route):
        raise exc
    return run


ROUTES = [DIRECT, Route("warp", proxy="socks5://127.0.0.1:40000")]


def test_chain_falls_through_routes_and_strategies(tmp_path):
    calls = []

    def blocked_on_direct(url, fmt, workdir, route):
        calls.append(route.name)
        if route.name == "directo":
            raise IPBlocked("403")
        return {"type": "video", "route": route.name}

    res = run_chain("TikTok", [Strategy("a", blocked_on_direct)], "u", "mp4", str(tmp_path), ROUTES)
    assert res["route"] == "warp" and calls == ["directo", "warp"]
    assert res["strategy"] == "a@warp"


def test_content_error_skips_other_routes_but_tries_next_strategy(tmp_path):
    calls = []

    def private(url, fmt, workdir, route):
        calls.append(route.name)
        raise Private("x")

    res = run_chain("Instagram", [Strategy("a", private), Strategy("b", _ok)], "u", "mp4", str(tmp_path), ROUTES)
    assert calls == ["directo"] and res["strategy"] == "b@directo"


def test_chain_raises_most_informative_error(tmp_path):
    strategies = [Strategy("a", _fail(IPBlocked("403"))), Strategy("b", _fail(RegionOrAgeLocked("10231")))]
    with pytest.raises(RegionOrAgeLocked):
        run_chain("TikTok", strategies, "u", "mp4", str(tmp_path), [DIRECT])


def test_anonymous_only_skips_auth_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANONYMOUS_ONLY", True)
    called = []
    auth = Strategy("login", lambda *a: called.append(1) or {"type": "video"}, requires_auth=True)
    with pytest.raises(Unknown):
        run_chain("Instagram", [auth], "u", "mp4", str(tmp_path), [DIRECT])
    assert not called


def test_route_insensitive_strategy_runs_once(tmp_path):
    calls = []

    def api(url, fmt, workdir, route):
        calls.append(route.name)
        raise IPBlocked("down")

    with pytest.raises(IPBlocked):
        run_chain("X", [Strategy("fx", api, route_sensitive=False)], "u", "mp4", str(tmp_path), ROUTES)
    assert calls == ["directo"]


def test_circuit_breaker_pauses_failing_strategy(tmp_path):
    strat = Strategy("flaky", _fail(IPBlocked("403")))
    for _ in range(health.FAILURE_THRESHOLD):
        with pytest.raises(IPBlocked):
            run_chain("TikTok", [strat], "u", "mp4", str(tmp_path), [DIRECT])
    assert health.is_open("flaky", "directo")
    # Content errors never trip it
    for _ in range(5):
        health.record_failure("other", "directo", "private", trips_breaker=False)
    assert not health.is_open("other", "directo")


# --- Network ----------------------------------------------------------------------
def test_random_ipv6_stays_inside_prefix():
    net = ipaddress.IPv6Network("2001:db8:1:2::/64")
    addrs = {random_ipv6(str(net)) for _ in range(50)}
    assert len(addrs) > 45
    assert all(ipaddress.IPv6Address(a) in net for a in addrs)


# --- Spotify ----------------------------------------------------------------------
def test_spotify_embed_parser():
    with open(os.path.join(HERE, "fixtures_spotify_album.html"), encoding="utf-8") as f:
        ent = parse_embed_page(f.read())
    assert ent["kind"] == "album"
    assert len(ent["tracks"]) >= 5
    first = ent["tracks"][0]
    assert first["title"] and first["artist"] and first["duration"] > 60
    assert ent["cover"].startswith("https://")


def test_spotify_matching_prefers_original_and_rejects_previews():
    track = {"title": "Song Name", "artist": "The Artist", "duration": 200}
    original = score_candidate(track, "The Artist - Song Name (Official Audio)", "The Artist", 201)
    remix = score_candidate(track, "Song Name (Remix)", "dj", 202)
    preview = score_candidate(track, "Song Name", "The Artist", 30)
    assert original > remix > 0
    assert preview == -1


# --- Instagram embed --------------------------------------------------------------
def test_instagram_embed_parser_fallback_regex():
    page = ('<div class="UsernameText">someone</div>'
            '<img class="EmbeddedMediaImage" alt="" src="https://cdn.example/img.jpg?a=1&amp;b=2">'
            '"video_url":"https:\\/\\/cdn.example\\/v.mp4"')
    parsed = parse_embed_html(page)
    assert parsed["author"] == "someone"
    assert parsed["media"][0] == {"type": "video", "url": "https://cdn.example/v.mp4",
                                  "thumbnail_url": "https://cdn.example/img.jpg?a=1&b=2"}


def test_instagram_embed_parser_no_media():
    assert parse_embed_html("<html>nothing</html>") is None


def test_igexport_parser():
    data = {"ok": True, "media": {"shortcode": "X", "items": [
        {"type": "video", "url": "https://cdn.example/v.mp4", "thumbnailUrl": "https://cdn.example/t.jpg"},
        {"type": "image", "url": "https://cdn.example/p.jpg"},
        {"type": "video"},
    ]}}
    assert media_from_igexport(data) == [
        {"type": "video", "url": "https://cdn.example/v.mp4", "thumbnail_url": "https://cdn.example/t.jpg"},
        {"type": "photo", "url": "https://cdn.example/p.jpg", "thumbnail_url": None},
    ]
    assert media_from_igexport({"ok": False}) == []


@pytest.mark.live
@pytest.mark.parametrize("url,fmt", [
    ("https://x.com/i/status/1585341984679469056", "mp4"),
    ("https://www.tiktok.com/@scout2015/video/6718335390845095173", "mp4"),
    ("https://www.instagram.com/p/aye83DjauH/", "mp4"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "mp4"),
    ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", "mp3"),
])
def test_live_download(url, fmt, tmp_path):
    from downloaders import VideoDownloader
    d = VideoDownloader(temp_dir=str(tmp_path))
    res = d._sync_download(url, fmt, base.new_workdir(str(tmp_path)))
    items = res.get("media_items") or [res]
    for item in items:
        assert os.path.getsize(item["file_path"]) > 10_000
        assert item["filesize"] <= config.MAX_TELEGRAM_SIZE_BYTES
