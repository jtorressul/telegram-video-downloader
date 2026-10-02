"""VIP verification by group title, using a fake Telegram bot."""
import asyncio
import os
import tempfile
import types

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="botdata_"))

import pytest  # noqa: E402

import bot  # noqa: E402
from db import UserDatabase  # noqa: E402

GROUP_A, GROUP_B = -100, -200
USER = 42


class FakeBot:
    """members[(chat_id, user_id)] = (status, custom_title) or an Exception to raise."""

    def __init__(self, members):
        self.members = members
        self.calls = 0
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        member = self.members.get((chat_id, user_id), ("left", None))
        if isinstance(member, Exception):
            raise member
        status, title = member
        return types.SimpleNamespace(status=status, custom_title=title)

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = UserDatabase(str(tmp_path / "users.db"))
    for gid in (GROUP_A, GROUP_B):
        database.register_group(gid)
    monkeypatch.setattr(bot, "user_db", database)
    monkeypatch.setattr(bot, "ADMIN_IDS", [])
    return database


def ctx(members):
    return types.SimpleNamespace(bot=FakeBot(members))


def make_stale(db):
    with db._get_connection() as conn:
        conn.execute("UPDATE users SET vip_checked_at = 0 WHERE user_id = ?", (USER,))


def is_vip(db):
    return bool(db.get_or_create_user(USER)["is_vip"])


def test_vip_title_in_group_grants_vip(db):
    c = ctx({(GROUP_A, USER): ("member", "Ana - VIP")})
    assert asyncio.run(bot.sync_user_vip_status(c, GROUP_A, USER)) is True
    assert is_vip(db)


def test_writing_in_other_group_keeps_vip_from_first_group(db):
    c = ctx({(GROUP_A, USER): ("member", "Ana - VIP"), (GROUP_B, USER): ("member", None)})
    asyncio.run(bot.sync_user_vip_status(c, GROUP_A, USER))
    make_stale(db)
    assert asyncio.run(bot.sync_user_vip_status(c, GROUP_B, USER)) is True
    assert is_vip(db)


def test_title_removed_revokes_vip_in_private_once_stale(db):
    c = ctx({(GROUP_A, USER): ("member", "Ana - VIP")})
    asyncio.run(bot.sync_user_vip_status(c, GROUP_A, USER))
    c.bot.members[(GROUP_A, USER)] = ("member", None)
    # Recent verification is trusted without calling Telegram
    calls = c.bot.calls
    assert asyncio.run(bot.sync_user_vip_from_all_groups(c, USER)) is True
    assert c.bot.calls == calls
    make_stale(db)
    assert asyncio.run(bot.sync_user_vip_from_all_groups(c, USER)) is False
    assert not is_vip(db)


def test_api_errors_never_revoke_vip(db):
    c = ctx({(GROUP_A, USER): ("member", "Ana - VIP")})
    asyncio.run(bot.sync_user_vip_status(c, GROUP_A, USER))
    make_stale(db)
    c.bot.members = {(GROUP_A, USER): RuntimeError("chat not found"),
                     (GROUP_B, USER): RuntimeError("network")}
    assert asyncio.run(bot.sync_user_vip_from_all_groups(c, USER)) is True
    assert asyncio.run(bot.sync_user_vip_status(c, GROUP_B, USER)) is True
    assert is_vip(db)


def test_daily_audit_revokes_and_notifies(db, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(bot.asyncio, "sleep", no_sleep)
    db.set_vip_status(USER, True)
    db.set_vip_status(7, True)
    fake = FakeBot({(GROUP_A, 7): ("member", "Leo - VIP")})
    app = types.SimpleNamespace(bot=fake)
    assert asyncio.run(bot.audit_vip_users(app)) == 1
    assert not is_vip(db)
    assert db.get_or_create_user(7)["is_vip"]
    assert [chat for chat, _ in fake.sent] == [USER]
