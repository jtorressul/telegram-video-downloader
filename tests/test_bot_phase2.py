"""Phase 2 bot features with fake Telegram + fake downloader: cancel, buttons, history, multi-link, inline."""
import asyncio
import os
import tempfile
import threading
import types

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="botdata_"))

import pytest  # noqa: E402

import bot  # noqa: E402
from cache import VideoCache  # noqa: E402
from db import UserDatabase  # noqa: E402
from downloaders import VideoDownloader  # noqa: E402
from downloaders.base import Strategy, run_chain  # noqa: E402
from downloaders.errors import Cancelled, IPBlocked, Private  # noqa: E402
from downloaders.net import DIRECT  # noqa: E402

USER = 42
OTHER_USER = 43
TIKTOK = "https://www.tiktok.com/@a/video/1"
TIKTOK2 = "https://www.tiktok.com/@a/video/2"


class FakeMessage:
    def __init__(self, bot_, text="", reply_markup=None):
        self.bot, self.text, self.reply_markup = bot_, text, reply_markup
        self.deleted = False
        self.video = types.SimpleNamespace(file_id=f"vid-{len(bot_.sent)}")
        self.audio = types.SimpleNamespace(file_id=f"aud-{len(bot_.sent)}")
        self.photo = [types.SimpleNamespace(file_id=f"pho-{len(bot_.sent)}")]

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.text, self.reply_markup = text, reply_markup

    async def delete(self):
        self.deleted = True

    async def reply_html(self, text, reply_markup=None, **kw):
        return await self.bot.send_message(0, text, reply_markup=reply_markup)


class FakeBot:
    def __init__(self):
        self.sent = []  # (kind, kwargs)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        msg = FakeMessage(self, text, reply_markup)
        self.sent.append(("message", msg))
        return msg

    async def _media(self, kind, **kw):
        msg = FakeMessage(self, kw.get("caption", ""), kw.get("reply_markup"))
        self.sent.append((kind, msg))
        return msg

    async def send_video(self, chat_id, video, **kw):
        if hasattr(video, "read"):
            video.read()
        return await self._media("video", **kw)

    async def send_audio(self, chat_id, audio, **kw):
        return await self._media("audio", **kw)

    async def send_photo(self, chat_id, photo, **kw):
        return await self._media("photo", **kw)

    async def send_chat_action(self, *a, **kw):
        return None

    async def send_media_group(self, chat_id, media, **kw):
        msgs = []
        for item in media:
            msg = FakeMessage(self, getattr(item, "caption", "") or "")
            is_video = type(item).__name__ == "InputMediaVideo"
            msg.video = msg.video if is_video else None
            msg.audio = None
            msg.photo = [] if is_video else msg.photo
            msg.media_ref = item.media if isinstance(item.media, str) else None
            self.sent.append(("album_item", msg))
            msgs.append(msg)
        return msgs

    async def get_file(self, file_id):
        self.sent.append(("get_file", file_id))

        async def download_to_drive(path):
            with open(path, "wb") as f:
                f.write(b"cached-video")
        return types.SimpleNamespace(download_to_drive=download_to_drive)

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")

    def texts(self):
        return [m.text for kind, m in self.sent if kind == "message"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "user_db", UserDatabase(str(tmp_path / "users.db")))
    monkeypatch.setattr(bot, "cache", VideoCache(str(tmp_path / "cache.db")))
    monkeypatch.setattr(bot, "ADMIN_IDS", [])
    monkeypatch.setattr(bot, "active_downloads", {})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake-video")
    state = types.SimpleNamespace(calls=[], gate=None, error=None)

    async def fake_download(url, format_type="mp4", cancel=None):
        state.calls.append((url, format_type))
        if state.gate:
            await state.gate.wait()
        if state.error:
            raise state.error
        return {"type": "video", "file_path": str(video), "title": f"Video {url[-1]}",
                "duration": 3, "filesize": 10}

    monkeypatch.setattr(bot.downloader, "download", fake_download)
    monkeypatch.setattr(bot.downloader, "cleanup", lambda result: None)
    fake = FakeBot()
    context = types.SimpleNamespace(bot=fake, bot_data={"username": "testbot"}, args=[])
    return types.SimpleNamespace(bot=fake, ctx=context, state=state, db=bot.user_db, cache=bot.cache)


def download(env, url=TIKTOK, fmt="mp4", **kw):
    return bot.execute_download(context=env.ctx, chat_id=1, user_id=USER, user_mention="@u",
                                url=url, format_type=fmt, **kw)


# --- URL helpers ----------------------------------------------------------------------
def test_unique_urls_dedupes_equivalent_links():
    text = ("https://www.instagram.com/reels/ABC/ https://instagram.com/reel/ABC/?igsh=1 "
            "https://youtu.be/jNQXAC9IVRw https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert len(bot.unique_urls(text)) == 2


# --- Solo audio / history ---------------------------------------------------------------
def test_video_has_audio_button_and_goes_to_history(env):
    assert asyncio.run(download(env)) is True
    kind, msg = next(s for s in env.bot.sent if s[0] == "video")
    button = msg.reply_markup.inline_keyboard[0][0]
    assert button.text == "🎵 Solo audio"
    assert env.cache.url_from_ref(int(button.callback_data.split(":")[1])) == TIKTOK
    history = env.db.get_history(USER)
    assert [(h["url"], h["format"]) for h in history] == [(TIKTOK, "mp4")]


def test_cached_delivery_keeps_audio_button_and_history_dedupes(env):
    asyncio.run(download(env))
    asyncio.run(download(env))  # second time comes from cache
    assert len(env.state.calls) == 1
    videos = [m for kind, m in env.bot.sent if kind == "video"]
    assert all(m.reply_markup for m in videos)
    assert len(env.db.get_history(USER)) == 1


# --- Retry -------------------------------------------------------------------------------
def test_retry_button_only_for_temporary_errors(env):
    env.state.error = IPBlocked("403")
    asyncio.run(download(env))
    status = [m for kind, m in env.bot.sent if kind == "message"][-1]
    assert status.reply_markup.inline_keyboard[0][0].callback_data.startswith("retry:mp4:")

    env.state.error = Private("private")
    asyncio.run(download(env, url=TIKTOK2))
    status = [m for kind, m in env.bot.sent if kind == "message"][-1]
    assert status.reply_markup is None


# --- Cancel ------------------------------------------------------------------------------
def test_cancel_stops_download_and_reports_it(env):
    async def scenario():
        env.state.gate = asyncio.Event()
        job = asyncio.create_task(download(env))
        await asyncio.sleep(0.05)
        status = env.bot.sent[0][1]
        assert status.reply_markup is bot.CANCEL_MARKUP
        assert bot.cancel_user_downloads(USER) == 1
        await job
        return status

    status = asyncio.run(scenario())
    assert "cancelada" in status.text
    assert not any(kind == "video" for kind, _ in env.bot.sent)
    assert env.db.get_quota(USER)[0] == 0
    assert bot.cancel_user_downloads(USER) == 0


def test_run_chain_stops_when_cancelled(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        run_chain("X", [Strategy("a", lambda *a: {"ok": 1})], "u", "mp4", str(tmp_path), [DIRECT], cancel=cancel)


def test_downloader_cancel_cleans_up_after_worker_finishes(tmp_path, monkeypatch):
    finished = threading.Event()

    def slow(url, fmt, workdir, cancel):
        cancel.wait(2)
        open(os.path.join(workdir, "partial.mp4"), "w").close()
        finished.set()
        raise Cancelled("stop")

    d = VideoDownloader(str(tmp_path))
    monkeypatch.setattr(d, "_sync_download", slow)

    async def scenario():
        task = asyncio.create_task(d.download("u"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(finished.wait, 2)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert os.listdir(tmp_path) == []


# --- Multiple links ----------------------------------------------------------------------
def test_multiple_links_stop_after_quota_denial(env, monkeypatch):
    monkeypatch.setattr(bot, "NO_VIP_DAILY_LIMIT", 1)
    monkeypatch.setattr("db.NO_VIP_DAILY_LIMIT", 1)
    msg = FakeMessage(env.bot, "x")
    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=1, type="private"),
        effective_user=types.SimpleNamespace(id=USER, mention_html=lambda: "@u"),
        message=msg)
    links = [TIKTOK, TIKTOK2, "https://www.tiktok.com/@a/video/3"]
    asyncio.run(bot.download_links(update, env.ctx, links))
    assert env.state.calls == [(TIKTOK, "mp4")]
    assert sum("Límite diario" in t for t in env.bot.texts()) == 1


# --- Inline ------------------------------------------------------------------------------
def _inline(env, text):
    answers = {}

    async def answer(results, **kw):
        answers.update(results=results, **kw)

    q = types.SimpleNamespace(query=text, from_user=types.SimpleNamespace(id=USER), answer=answer)
    asyncio.run(bot.inline_query_handler(types.SimpleNamespace(inline_query=q), env.ctx))
    return answers


def test_inline_uncached_offers_private_download(env):
    a = _inline(env, TIKTOK)
    assert a["results"] == []
    ref = int(a["button"].start_parameter.removeprefix("dl_"))
    assert env.cache.url_from_ref(ref) == TIKTOK


def test_inline_cached_returns_video_instantly(env):
    asyncio.run(download(env))
    a = _inline(env, f"mira {TIKTOK}")
    assert [type(r).__name__ for r in a["results"]] == ["InlineQueryResultCachedVideo"]
    assert a["is_personal"] is True


def test_inline_vip_platform_blocked_for_free_users(env):
    a = _inline(env, "https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert a["results"] == [] and "VIP" in a["button"].text


# --- Phase 4: carousels cached, concurrent dedupe, MP3 from cached MP4 ----------------------
def _album_result(tmp_path):
    files = []
    for i, kind in enumerate(["photo", "video", "photo"]):
        f = tmp_path / f"item{i}"
        f.write_bytes(b"x")
        files.append({"type": kind, "file_path": str(f)})
    return {"type": "carousel", "media_items": files, "title": "Carrusel", "filesize": 30}


def test_carousel_is_cached_and_resent_by_file_id(env, tmp_path, monkeypatch):
    async def album_download(url, format_type="mp4", cancel=None):
        env.state.calls.append((url, format_type))
        return _album_result(tmp_path)
    monkeypatch.setattr(bot.downloader, "download", album_download)

    asyncio.run(download(env))
    asyncio.run(download(env))
    assert len(env.state.calls) == 1
    resent = [m for kind, m in env.bot.sent if kind == "album_item"][3:]
    assert len(resent) == 3 and all(m.media_ref for m in resent)
    assert "instantánea" in resent[0].text
    assert env.db.get_quota(USER)[0] == 1  # second time came from cache: no quota


def test_inline_skips_cached_albums(env, tmp_path, monkeypatch):
    async def album_download(url, format_type="mp4", cancel=None):
        return _album_result(tmp_path)
    monkeypatch.setattr(bot.downloader, "download", album_download)
    asyncio.run(download(env))
    a = _inline(env, TIKTOK)
    assert a["results"] == [] and a["button"].start_parameter.startswith("dl_")


def test_concurrent_requests_for_same_link_download_once(env):
    async def scenario():
        env.state.gate = asyncio.Event()
        first = asyncio.create_task(download(env))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(bot.execute_download(
            context=env.ctx, chat_id=2, user_id=OTHER_USER, user_mention="@o", url=TIKTOK, format_type="mp4"))
        await asyncio.sleep(0.05)
        assert any("ya se está descargando" in t for t in env.bot.texts())
        env.state.gate.set()
        return await asyncio.gather(first, second)

    assert asyncio.run(scenario()) == [True, True]
    assert len(env.state.calls) == 1
    assert sum(kind == "video" for kind, _ in env.bot.sent) == 2
    assert env.db.get_quota(OTHER_USER)[0] == 0  # served from the first download's cache
    assert bot.inflight_downloads == {}


def test_waiter_downloads_itself_when_first_download_fails(env, monkeypatch):
    real_download = bot.downloader.download

    async def first_call_fails(url, format_type="mp4", cancel=None):
        env.state.error = IPBlocked("403") if not env.state.calls else None
        return await real_download(url, format_type, cancel)
    monkeypatch.setattr(bot.downloader, "download", first_call_fails)

    async def scenario():
        env.state.gate = asyncio.Event()
        first = asyncio.create_task(download(env))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(bot.execute_download(
            context=env.ctx, chat_id=2, user_id=OTHER_USER, user_mention="@o", url=TIKTOK, format_type="mp4"))
        await asyncio.sleep(0.05)
        env.state.gate.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert len(env.state.calls) == 2
    assert sum(kind == "video" for kind, _ in env.bot.sent) == 1


def test_mp3_reuses_cached_video_without_platform_download(env, monkeypatch):
    def fake_to_mp3(src, dst):
        with open(dst, "wb") as f:
            f.write(b"mp3")
        return dst
    monkeypatch.setattr(bot, "to_mp3", fake_to_mp3)
    asyncio.run(download(env))           # mp4 from the platform
    asyncio.run(download(env, fmt="mp3"))  # mp3 from the cached mp4
    assert env.state.calls == [(TIKTOK, "mp4")]
    assert ("get_file", "vid-0") in env.bot.sent or any(k == "get_file" for k, _ in env.bot.sent)
    assert any(kind == "audio" for kind, _ in env.bot.sent)
    assert env.cache.get(TIKTOK, "mp3")["media_type"] == "audio"


def test_mp3_falls_back_to_platform_for_big_cached_video(env, monkeypatch):
    monkeypatch.setattr(bot, "BOT_API_DOWNLOAD_LIMIT", 1)
    asyncio.run(download(env))
    asyncio.run(download(env, fmt="mp3"))
    assert env.state.calls == [(TIKTOK, "mp4"), (TIKTOK, "mp3")]
    assert not any(kind == "get_file" for kind, _ in env.bot.sent)
