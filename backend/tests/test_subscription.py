from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember
from aiogram.types import User as TgUser

from pca.services.subscription import MembershipStatus, MemoryCache, SubscriptionConfig, SubscriptionService

CONFIG = SubscriptionConfig(
    enforced=True, channel_id=-100123, channel_url="https://t.me/x", channel_username="x"
)
TG_USER = TgUser(id=42, is_bot=False, first_name="Test")
METHOD = GetChatMember(chat_id=-100123, user_id=42)


class FakeFetcher:
    def __init__(self, result=None, error: Exception | None = None, delay: float = 0) -> None:
        self.result, self.error, self.delay, self.calls = result, error, delay, 0

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


async def _check(fetcher, *, cache=None, is_admin=False, config=CONFIG, fresh=False, timeout=5.0):
    service = SubscriptionService(fetcher, cache, cache_ttl_seconds=60, request_timeout_seconds=timeout)
    return await service.check(telegram_user_id=42, is_admin=is_admin, config=config, fresh=fresh)


async def test_member_is_allowed():
    result = await _check(FakeFetcher(ChatMemberMember(user=TG_USER)))
    assert result.status is MembershipStatus.SUBSCRIBED and result.allowed


async def test_channel_admin_is_member():
    required_flags = {
        name: False
        for name, field in ChatMemberAdministrator.model_fields.items()
        if field.is_required() and name not in ("user", "status")
    }
    member = ChatMemberAdministrator(user=TG_USER, **required_flags)
    assert (await _check(FakeFetcher(member))).allowed


async def test_left_user_is_blocked():
    result = await _check(FakeFetcher(ChatMemberLeft(user=TG_USER)))
    assert result.status is MembershipStatus.NOT_SUBSCRIBED and not result.allowed


async def test_participant_not_found_is_not_subscribed():
    err = TelegramBadRequest(method=METHOD, message="Bad Request: user not found")
    assert (await _check(FakeFetcher(error=err))).status is MembershipStatus.NOT_SUBSCRIBED


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(method=METHOD, message="Forbidden: bot is not a member"),
        TelegramBadRequest(method=METHOD, message="Bad Request: chat not found"),
        TelegramNetworkError(method=METHOD, message="connection reset"),
    ],
)
async def test_telegram_failures_fail_closed(error):
    result = await _check(FakeFetcher(error=error))
    assert result.status is MembershipStatus.UNAVAILABLE and not result.allowed


async def test_timeout_fails_closed():
    result = await _check(FakeFetcher(ChatMemberMember(user=TG_USER), delay=1), timeout=0.05)
    assert result.status is MembershipStatus.UNAVAILABLE and result.reason == "telegram_unreachable"


async def test_admin_bypasses_without_calling_telegram():
    fetcher = FakeFetcher(error=RuntimeError("must not be called"))
    assert (await _check(fetcher, is_admin=True)).status is MembershipStatus.BYPASSED
    assert fetcher.calls == 0


async def test_missing_channel_id_fails_closed():
    config = SubscriptionConfig(enforced=True, channel_id=None, channel_url="", channel_username="")
    assert not (await _check(FakeFetcher(ChatMemberMember(user=TG_USER)), config=config)).allowed


async def test_not_enforced_allows():
    config = SubscriptionConfig(enforced=False, channel_id=None, channel_url="", channel_username="")
    assert (await _check(FakeFetcher(), config=config)).status is MembershipStatus.NOT_ENFORCED


async def test_positive_results_are_cached_and_fresh_bypasses_cache():
    cache = MemoryCache()
    fetcher = FakeFetcher(ChatMemberMember(user=TG_USER))
    await _check(fetcher, cache=cache)
    await _check(fetcher, cache=cache)
    assert fetcher.calls == 1

    fetcher.result = ChatMemberLeft(user=TG_USER)
    assert (await _check(fetcher, cache=cache, fresh=True)).status is MembershipStatus.NOT_SUBSCRIBED
    assert fetcher.calls == 2


async def test_negative_results_are_not_cached():
    cache = MemoryCache()
    fetcher = FakeFetcher(ChatMemberLeft(user=TG_USER))
    await _check(fetcher, cache=cache)
    fetcher.result = ChatMemberMember(user=TG_USER)
    assert (await _check(fetcher, cache=cache)).allowed
    assert fetcher.calls == 2


async def test_broken_cache_falls_back_to_live_check():
    class BrokenCache:
        async def get(self, key):
            raise ConnectionError

        async def set(self, key, value, ttl_seconds):
            raise ConnectionError

    assert (await _check(FakeFetcher(ChatMemberMember(user=TG_USER)), cache=BrokenCache())).allowed
