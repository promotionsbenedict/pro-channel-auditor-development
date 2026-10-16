"""Translate Telethon exceptions into stable, user-safe error codes.

Raw Telegram error text is logged but never shown to customers: it can contain
internal details and changes between layers, whereas these codes are part of
our API contract with the connect page and bot handlers.
"""

from __future__ import annotations

import asyncio

from telethon import errors


class TelegramGatewayError(Exception):
    def __init__(self, code: str, *, retry_after: int | None = None, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.detail = detail


class SessionInvalidError(TelegramGatewayError):
    """The stored authorization no longer works (logged out, terminated from Devices, banned)."""


_SESSION_DEAD: tuple[type[BaseException], ...] = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyDuplicatedError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)

_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (errors.PhoneNumberInvalidError, "phone_invalid"),
    (errors.PhoneNumberBannedError, "phone_banned"),
    (errors.PhoneNumberUnoccupiedError, "not_registered"),
    (errors.PhoneNumberFloodError, "flood"),
    (errors.PhonePasswordFloodError, "password_flood"),
    (errors.PhoneCodeInvalidError, "code_invalid"),
    (errors.PhoneCodeEmptyError, "code_invalid"),
    (errors.PhoneCodeExpiredError, "code_expired"),
    (errors.PhoneCodeHashEmptyError, "code_expired"),
    (errors.SendCodeUnavailableError, "resend_unavailable"),
    (errors.PasswordHashInvalidError, "password_invalid"),
    (errors.SrpIdInvalidError, "password_challenge_expired"),
    (errors.ApiIdInvalidError, "service_misconfigured"),
    (errors.ApiIdPublishedFloodError, "service_misconfigured"),
    (errors.ChannelPrivateError, "channel_unavailable"),
    (errors.ChannelInvalidError, "channel_unavailable"),
    (errors.ChatAdminRequiredError, "insufficient_rights"),
)


def translate(exc: BaseException) -> TelegramGatewayError:
    if isinstance(exc, TelegramGatewayError):
        return exc
    detail = type(exc).__name__
    if isinstance(exc, _SESSION_DEAD):
        return SessionInvalidError("session_invalid", detail=detail)
    if isinstance(exc, errors.FloodError):
        seconds = getattr(exc, "seconds", None)
        return TelegramGatewayError("flood", retry_after=int(seconds) if seconds else None, detail=detail)
    for cls, code in _CODES:
        if isinstance(exc, cls):
            return TelegramGatewayError(code, detail=detail)
    if isinstance(exc, errors.RPCError):
        return TelegramGatewayError("telegram_error", detail=f"{detail}:{getattr(exc, 'message', '')}")
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return TelegramGatewayError("network", detail=detail)
    return TelegramGatewayError("unexpected", detail=detail)
