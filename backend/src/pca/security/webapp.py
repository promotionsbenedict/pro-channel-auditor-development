"""Verify Telegram Mini App initData so every connect request is tied to a real Telegram user."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram.utils.web_app import safe_parse_webapp_init_data

_MAX_LENGTH = 4096
_FUTURE_SKEW_SECONDS = 60


class InitDataError(Exception):
    pass


@dataclass(frozen=True)
class WebAppIdentity:
    telegram_user_id: int
    username: str | None
    auth_date: datetime


def verify_init_data(
    init_data: str, bot_token: str, *, max_age_seconds: int, now: datetime | None = None
) -> WebAppIdentity:
    if not init_data or len(init_data) > _MAX_LENGTH or not bot_token:
        raise InitDataError("missing")
    try:
        data = safe_parse_webapp_init_data(token=bot_token, init_data=init_data)
    except ValueError as exc:
        raise InitDataError("signature") from exc
    if data.user is None:
        raise InitDataError("no_user")
    auth_date = data.auth_date if data.auth_date.tzinfo else data.auth_date.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - auth_date).total_seconds()
    if age > max_age_seconds or age < -_FUTURE_SKEW_SECONDS:
        raise InitDataError("expired")
    return WebAppIdentity(telegram_user_id=data.user.id, username=data.user.username, auth_date=auth_date)
