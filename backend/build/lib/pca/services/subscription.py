"""Mandatory channel-subscription verification via Bot API getChatMember.

Design rules:
- Button clicks are never proof; every decision is a live or briefly cached getChatMember result.
- Sensitive operations pass fresh=True to bypass the cache.
- Telegram/network failures yield UNAVAILABLE (retryable), never NOT_SUBSCRIBED.
- Misconfiguration (missing channel id) fails closed as UNAVAILABLE.
- Only positive results are cached, so a user who just joined is never stuck.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
)

logger = logging.getLogger(__name__)

_NOT_A_MEMBER_ERRORS = ("user not found", "participant_id_invalid", "user_not_participant")


class MembershipStatus(StrEnum):
    SUBSCRIBED = "subscribed"
    NOT_SUBSCRIBED = "not_subscribed"
    UNAVAILABLE = "unavailable"
    BYPASSED = "bypassed"
    NOT_ENFORCED = "not_enforced"


@dataclass(frozen=True)
class MembershipResult:
    status: MembershipStatus
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status in (MembershipStatus.SUBSCRIBED, MembershipStatus.BYPASSED, MembershipStatus.NOT_ENFORCED)


@dataclass(frozen=True)
class SubscriptionConfig:
    enforced: bool
    channel_id: int | None
    channel_url: str
    channel_username: str
    gate_text: str | None = None


class MembershipFetcher(Protocol):
    async def get_chat_member(self, chat_id: int, user_id: int): ...


class MembershipCache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = value


class RedisCache:
    def __init__(self, redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(key)
        return value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._redis.set(key, value, ex=ttl_seconds)


def is_member(member: object) -> bool:
    if isinstance(member, ChatMemberOwner | ChatMemberAdministrator | ChatMemberMember):
        return True
    if isinstance(member, ChatMemberRestricted):
        return bool(member.is_member)
    if isinstance(member, ChatMemberLeft | ChatMemberBanned):
        return False
    return False


class SubscriptionService:
    def __init__(
        self,
        fetcher: MembershipFetcher,
        cache: MembershipCache | None = None,
        *,
        cache_ttl_seconds: int = 120,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self._fetcher = fetcher
        self._cache = cache
        self._ttl = cache_ttl_seconds
        self._timeout = request_timeout_seconds

    @staticmethod
    def _cache_key(channel_id: int, telegram_user_id: int) -> str:
        return f"pca:sub:{channel_id}:{telegram_user_id}"

    async def _cache_get(self, key: str) -> str | None:
        if self._cache is None or self._ttl <= 0:
            return None
        try:
            return await self._cache.get(key)
        except Exception:
            logger.warning("subscription cache read failed; falling back to live check")
            return None

    async def _cache_set(self, key: str) -> None:
        if self._cache is None or self._ttl <= 0:
            return
        try:
            await self._cache.set(key, "1", self._ttl)
        except Exception:
            logger.warning("subscription cache write failed")

    async def check(
        self,
        *,
        telegram_user_id: int,
        is_admin: bool,
        config: SubscriptionConfig,
        fresh: bool = False,
    ) -> MembershipResult:
        if is_admin:
            return MembershipResult(MembershipStatus.BYPASSED)
        if not config.enforced:
            return MembershipResult(MembershipStatus.NOT_ENFORCED)
        if config.channel_id is None:
            logger.error("subscription enforced but no required channel id is configured")
            return MembershipResult(MembershipStatus.UNAVAILABLE, "not_configured")

        key = self._cache_key(config.channel_id, telegram_user_id)
        if not fresh and await self._cache_get(key) == "1":
            return MembershipResult(MembershipStatus.SUBSCRIBED, "cached")

        try:
            member = await asyncio.wait_for(
                self._fetcher.get_chat_member(chat_id=config.channel_id, user_id=telegram_user_id),
                timeout=self._timeout,
            )
        except TelegramBadRequest as exc:
            if any(marker in str(exc.message).lower() for marker in _NOT_A_MEMBER_ERRORS):
                return MembershipResult(MembershipStatus.NOT_SUBSCRIBED, "not_participant")
            logger.error("getChatMember rejected for required channel: %s", exc.message)
            return MembershipResult(MembershipStatus.UNAVAILABLE, "bad_request")
        except TelegramForbiddenError as exc:
            logger.error("bot lacks access to required channel: %s", exc.message)
            return MembershipResult(MembershipStatus.UNAVAILABLE, "bot_forbidden")
        except TelegramRetryAfter:
            return MembershipResult(MembershipStatus.UNAVAILABLE, "rate_limited")
        except (TelegramNetworkError, TelegramServerError, TimeoutError):
            return MembershipResult(MembershipStatus.UNAVAILABLE, "telegram_unreachable")
        except TelegramAPIError as exc:
            logger.error("unexpected Telegram error during membership check: %s", exc.message)
            return MembershipResult(MembershipStatus.UNAVAILABLE, "telegram_error")

        if is_member(member):
            await self._cache_set(key)
            return MembershipResult(MembershipStatus.SUBSCRIBED)
        return MembershipResult(MembershipStatus.NOT_SUBSCRIBED, getattr(member, "status", None))
