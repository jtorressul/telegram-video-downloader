"""Phase 3 admin commands: activity log, /estado, /top, /viplist, bans and broadcast."""
import asyncio
import os
import tempfile
import time
import types

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="botdata_"))

import pytest  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

import bot  # noqa: E402
from db import UserDatabase, get_local_today_str  # noqa: E402

ADMIN, USER, OTHER = 1, 42, 43


class FakeMessage:
    def __init__(self, text="", reply_to=None):
        self.text, self.reply_to_message, self.replies = text, reply_to, []

    async def reply_html(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))

    async def reply_text(self, text, **kw):
        self.replies.append((text, None))


class FakeBot:
    def __init__(self, fail_for=()):
        self.fail_for, self.delivered, self.messages = set(fail_for), [], []

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.messages.append((chat_id, text))

    async def copy_message(self, chat_id, from_chat_id, message_id):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden")
        self.delivered.append((chat_id, from_chat_id, message_id))


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = UserDatabase(str(tmp_path / "users.db"))
    monkeypatch.setattr(bot, "user_db", database)
    monkeypatch.setattr(bot, "ADMIN_IDS", [ADMIN])
    monkeypatch.setattr(bot, "active_downloads", {})
    monkeypatch.setattr(bot, "BROADCAST_DELAY_S", 0)
    return database


def command(user_id, text="", args=None, reply_to=None, chat_type="private"):
    msg = FakeMessage(text, reply_to)
    update = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id),
        effective_chat=types.SimpleNamespace(id=user_id, type=chat_type),
        message=msg, callback_query=None, inline_query=None)
    context = types.SimpleNamespace(args=args or [], bot_data={}, bot=FakeBot())
    return update, context, msg


# --- Activity log ---------------------------------------------------------------------------
def test_platform_stats_and_top(db):
    db.get_or_create_user(USER, "ana", "Ana")
    db.log_download(USER, "TikTok", ok=True)
    db.log_download(USER, "TikTok", ok=True, cached=True)
    db.log_download(USER, "Instagram", ok=False)
    db.log_download(OTHER, "TikTok", ok=True)
    stats = {s["platform"]: s for s in db.platform_stats(get_local_today_str())}
    assert (stats["TikTok"]["ok"], stats["TikTok"]["cached"], stats["TikTok"]["failed"]) == (3, 1, 0)
    assert stats["Instagram"]["failed"] == 1
    top = db.top_users(time.time() - 3600)
    assert [(r["user_id"], r["downloads"]) for r in top] == [(USER, 2), (OTHER, 1)]
    assert top[0]["username"] == "ana"


def test_prune_removes_old_entries_only(db):
    db.log_download(USER, "TikTok", ok=True)
    with db._get_connection() as conn:
        conn.execute("INSERT INTO download_log (day, user_id, platform, ok, created_at) "
                     "VALUES ('2020-01-01', 1, 'X', 1, 0)")
    assert db.prune_download_log() == 1
    assert len(db.platform_stats(get_local_today_str())) == 1


# --- Admin-only access ---------------------------------------------------------------------
@pytest.mark.parametrize("handler", ["estado_command", "top_command", "viplist_command", "ban_cmd", "broadcast_command"])
def test_admin_commands_ignore_regular_users(db, handler):
    update, context, msg = command(USER, "/x hola", args=[str(OTHER)])
    asyncio.run(getattr(bot, handler)(update, context))
    assert msg.replies == []


def test_estado_summarizes_today(db, monkeypatch):
    monkeypatch.setattr(bot, "igexport_status", lambda: "✅ responde (0.1 s)")
    db.log_download(USER, "TikTok", ok=True, cached=True)
    db.log_download(USER, "YouTube", ok=False)
    update, context, msg = command(ADMIN)
    asyncio.run(bot.estado_command(update, context))
    text = msg.replies[0][0]
    assert "1 ✅ (1 desde caché) · 1 ❌" in text
    assert "YouTube: 0 ✅ · 1 ❌" in text and "igexport" in text


def test_viplist_excludes_bot_admins(db):
    db.set_vip_status(ADMIN, True)
    db.set_vip_status(USER, True, "ana")
    update, context, msg = command(ADMIN)
    asyncio.run(bot.viplist_command(update, context))
    text = msg.replies[0][0]
    assert "@ana" in text and f"<code>{ADMIN}</code>" not in text and "VIP (1)" in text


# --- Bans --------------------------------------------------------------------------------
def test_ban_by_reply_blocks_user_everywhere(db):
    target = types.SimpleNamespace(from_user=types.SimpleNamespace(id=USER))
    update, context, msg = command(ADMIN, reply_to=target)
    asyncio.run(bot.ban_cmd(update, context))
    assert db.is_banned(USER)

    banned_update, banned_ctx, banned_msg = command(USER, "https://tiktok.com/x")
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.ban_gate(banned_update, banned_ctx))
    assert "No tienes acceso" in banned_msg.replies[0][0]

    update, context, _ = command(ADMIN, args=[str(USER)])
    asyncio.run(bot.unban_cmd(update, context))
    assert not db.is_banned(USER)
    ok_update, ok_ctx, _ = command(USER)
    asyncio.run(bot.ban_gate(ok_update, ok_ctx))  # no exception: passes through


def test_admins_cannot_be_banned(db):
    update, context, msg = command(ADMIN, args=[str(ADMIN)])
    asyncio.run(bot.ban_cmd(update, context))
    assert not db.is_banned(ADMIN) and "No se puede" in msg.replies[0][0]


# --- Broadcast ---------------------------------------------------------------------------
def test_broadcast_requires_confirmation_and_skips_banned(db):
    for uid in (USER, OTHER, 44):
        db.get_or_create_user(uid)
    db.set_banned(44, True)

    update, context, msg = command(ADMIN, "/broadcast Hola a todos")
    asyncio.run(bot.broadcast_command(update, context))
    text, markup = msg.replies[0]
    assert "a 2 usuarios" in text  # USER and OTHER; banned 44 excluded
    assert context.bot_data["broadcast_pending"][ADMIN] == {"text": "Hola a todos"}

    fake = FakeBot(fail_for={OTHER})
    sent, failed = asyncio.run(bot.run_broadcast(fake, ADMIN, {"text": "Hola a todos"}))
    assert (sent, failed) == (1, 1)
    assert (USER, "Hola a todos") in fake.messages
    assert "1 entregados" in fake.messages[-1][1]


def test_broadcast_reply_copies_original_message(db):
    db.get_or_create_user(USER)
    fake = FakeBot()
    asyncio.run(bot.run_broadcast(fake, ADMIN, {"from_chat_id": ADMIN, "message_id": 99}))
    assert fake.delivered == [(USER, ADMIN, 99)]
