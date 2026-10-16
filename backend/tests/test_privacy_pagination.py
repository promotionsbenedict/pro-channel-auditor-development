from __future__ import annotations

from sqlalchemy import select

from pca.bot.keyboards import PAGE_SIZE, ChanCb, PageCb, channel_list, page_bounds
from pca.db.models import ChannelAuthorization, TelegramConnection, User
from pca.domain.enums import ConnectionStatus, UserStatus
from pca.services import users as user_service

from .test_audit_engine import _setup, keyring, settings  # noqa: F401  (fixtures)


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_page_bounds_clamps_out_of_range_pages():
    assert page_bounds(0, 5) == (0, 0, 0)
    assert page_bounds(PAGE_SIZE * 2 + 3, 99) == (2, PAGE_SIZE * 2, PAGE_SIZE * 2 + 3)
    assert page_bounds(PAGE_SIZE * 2 + 3, -4) == (0, 0, PAGE_SIZE)


def test_channel_list_reaches_every_channel_across_pages():
    total = PAGE_SIZE * 3 + 2
    items = [(i, f"Channel {i}", i % 2 == 0) for i in range(1, total + 1)]
    seen: set[int] = set()
    page = 0
    for _ in range(10):
        buttons = _buttons(channel_list(items, "audit", page))
        for b in buttons:
            if b.callback_data and b.callback_data.startswith("ch:"):
                seen.add(ChanCb.unpack(b.callback_data).id)
        nxt = [b for b in buttons if b.text.startswith("Next")]
        if not nxt:
            break
        page = PageCb.unpack(nxt[0].callback_data).page
    assert seen == set(range(1, total + 1))


def test_single_page_has_no_navigation():
    buttons = _buttons(channel_list([(1, "Only", True)], "view"))
    assert not any(b.text.startswith(("Next", "‹ Prev")) for b in buttons)


async def test_deletion_wipes_sessions_and_channel_access(sessionmaker, settings, keyring):  # noqa: F811
    user_id, _ = await _setup(sessionmaker, settings, keyring, 5001, pending=3)
    other_id, _ = await _setup(sessionmaker, settings, keyring, 5002, pending=3)

    async with sessionmaker() as s, s.begin():
        user = await s.get(User, user_id)
        await user_service.request_deletion(s, user)

    async with sessionmaker() as s:
        user = await s.get(User, user_id)
        assert user.status == UserStatus.DELETION_REQUESTED.value
        assert user.deletion_requested_at is not None

        mine = (await s.execute(select(TelegramConnection).where(TelegramConnection.user_id == user_id))).scalars().all()
        assert mine and all(c.connection_status == ConnectionStatus.REVOKED.value for c in mine)
        assert all(c.encrypted_session is None and c.encryption_key_id is None for c in mine)

        my_auths = (
            await s.execute(select(ChannelAuthorization).where(ChannelAuthorization.user_id == user_id))
        ).scalars().all()
        assert my_auths and all(a.authorization_status != "verified" for a in my_auths)

        theirs = (
            await s.execute(select(TelegramConnection).where(TelegramConnection.user_id == other_id))
        ).scalar_one()
        assert theirs.connection_status == ConnectionStatus.ACTIVE.value
        assert theirs.encrypted_session is not None
