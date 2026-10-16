"""Account-connection flow: one single-use link → phone → code → (2FA) → stored session.

Every step locks the flow row, so double-submits from the browser are
serialized. In-flight login state (the not-yet-authorized MTProto session and
phone_code_hash) is encrypted with a purpose-separated key and bound to the
flow id. The customer's 2FA password never reaches this server: the browser
computes the SRP proof locally and sends only that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pca.config import Settings
from pca.db.models import ConnectFlow, TelegramConnection, User
from pca.domain.enums import (
    OPEN_CONNECT_FLOW_STATUSES,
    ConnectFlowStatus,
    ConnectionStatus,
    UserStatus,
)
from pca.security.crypto import DecryptionError, Keyring, Purpose, Sealed, pending_login_aad
from pca.services.connection import (
    active_connections_for_update,
    open_session,
    revoke_channel_authorizations,
    seal_session,
)
from pca.telegram.errors import SessionInvalidError, TelegramGatewayError
from pca.telegram.types import LoginGateway, SignInResult

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_PHONE_STRIP_RE = re.compile(r"[\s\-().]")
_PHONE_RE = re.compile(r"^\d{7,15}$")
_CODE_RE = re.compile(r"^\d{4,8}$")
_FATAL_CODES = {
    "phone_banned",
    "not_registered",
    "service_misconfigured",
    "payment_required",
    "email_setup_required",
    "unsupported_delivery",
    "unsupported_password",
    "password_flood",
}
_TRANSIENT_CODES = {"network", "telegram_error", "unexpected"}


class ConnectError(Exception):
    def __init__(self, code: str, status: int = 400, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect_url(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/connect?t={token}"


def normalize_phone(raw: str) -> str:
    phone = _PHONE_STRIP_RE.sub("", raw or "")
    if phone.startswith("00"):
        phone = phone[2:]
    phone = phone.removeprefix("+")
    if not _PHONE_RE.fullmatch(phone):
        raise ConnectError("phone_invalid")
    return phone


def mask_phone(phone: str) -> str:
    return f"+{phone[:3]}{'•' * max(len(phone) - 5, 2)}{phone[-2:]}"


def _finish(flow: ConnectFlow, status: ConnectFlowStatus, reason: str | None = None) -> None:
    flow.status = status.value
    flow.encrypted_state = None
    flow.encryption_key_id = None
    flow.failure_reason = reason
    if status == ConnectFlowStatus.COMPLETED:
        flow.completed_at = _now()


def _store_state(keyring: Keyring, flow: ConnectFlow, user: User, state: dict[str, Any]) -> None:
    sealed = keyring.seal(
        json.dumps(state).encode(), purpose=Purpose.PENDING_LOGIN, aad=pending_login_aad(user.id, flow.flow_id)
    )
    flow.encrypted_state, flow.encryption_key_id = sealed.blob, sealed.key_id


def _load_state(keyring: Keyring, flow: ConnectFlow, user: User) -> dict[str, Any]:
    if not flow.encrypted_state or not flow.encryption_key_id:
        _finish(flow, ConnectFlowStatus.FAILED, "state_missing")
        raise ConnectError("link_expired", 410)
    try:
        raw = keyring.open(
            Sealed(flow.encryption_key_id, flow.encrypted_state),
            purpose=Purpose.PENDING_LOGIN,
            aad=pending_login_aad(user.id, flow.flow_id),
        )
    except DecryptionError as exc:
        _finish(flow, ConnectFlowStatus.FAILED, "state_unreadable")
        raise ConnectError("link_expired", 410) from exc
    return json.loads(raw)


def _gateway_error(flow: ConnectFlow, exc: TelegramGatewayError) -> ConnectError:
    if exc.code == "flood":
        return ConnectError("flood", 429, retry_after=exc.retry_after)
    if exc.code in _FATAL_CODES:
        _finish(flow, ConnectFlowStatus.FAILED, exc.code)
        return ConnectError(exc.code, 400)
    if exc.code in _TRANSIENT_CODES or isinstance(exc, SessionInvalidError):
        log.warning("telegram login step failed", extra={"code": exc.code, "detail": exc.detail})
        return ConnectError("telegram_unavailable", 503)
    return ConnectError(exc.code, 400)


def view(flow: ConnectFlow, **extra: Any) -> dict[str, Any]:
    return {
        "status": flow.status,
        "phone_masked": flow.phone_masked,
        "attempts_left": None,
        "expires_at": flow.expires_at.isoformat() if flow.expires_at else None,
        **extra,
    }


async def _close_open_flows(session: AsyncSession, user_id: int, status: ConnectFlowStatus, reason: str) -> None:
    await session.execute(
        update(ConnectFlow)
        .where(ConnectFlow.user_id == user_id, ConnectFlow.status.in_(OPEN_CONNECT_FLOW_STATUSES))
        .values(status=status.value, encrypted_state=None, encryption_key_id=None, failure_reason=reason)
    )


async def expire_stale_flows(session: AsyncSession) -> int:
    result = await session.execute(
        update(ConnectFlow)
        .where(ConnectFlow.status.in_(OPEN_CONNECT_FLOW_STATUSES), ConnectFlow.expires_at < func.now())
        .values(
            status=ConnectFlowStatus.EXPIRED.value,
            encrypted_state=None,
            encryption_key_id=None,
            failure_reason="expired",
        )
    )
    return result.rowcount or 0


async def issue_link(session: AsyncSession, settings: Settings, user: User) -> str:
    """Create a single-use connect link, superseding any earlier unfinished one."""
    await _close_open_flows(session, user.id, ConnectFlowStatus.CANCELLED, "superseded")
    token = secrets.token_urlsafe(32)
    session.add(
        ConnectFlow(
            flow_id=secrets.token_hex(16),
            link_token_hash=hash_token(token),
            user_id=user.id,
            status=ConnectFlowStatus.ISSUED.value,
            expires_at=_now() + timedelta(seconds=settings.connect_link_ttl_seconds),
        )
    )
    await session.flush()
    return token


async def _load(session: AsyncSession, token: str, telegram_user_id: int) -> tuple[ConnectFlow, User]:
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ConnectError("link_invalid", 404)
    row = (
        await session.execute(
            select(ConnectFlow, User)
            .join(User, User.id == ConnectFlow.user_id)
            .where(ConnectFlow.link_token_hash == hash_token(token))
            .with_for_update(of=ConnectFlow)
        )
    ).one_or_none()
    if row is None:
        raise ConnectError("link_invalid", 404)
    flow, user = row
    if user.telegram_user_id != telegram_user_id:
        raise ConnectError("wrong_user", 403)
    if user.status != UserStatus.ACTIVE.value:
        raise ConnectError("account_unavailable", 403)
    if flow.status in OPEN_CONNECT_FLOW_STATUSES and flow.expires_at <= _now():
        _finish(flow, ConnectFlowStatus.EXPIRED, "expired")
        raise ConnectError("link_expired", 410)
    return flow, user


def _require(flow: ConnectFlow, *statuses: ConnectFlowStatus) -> None:
    if flow.status in {s.value for s in statuses}:
        return
    if flow.status not in OPEN_CONNECT_FLOW_STATUSES:
        raise ConnectError("link_used", 410)
    raise ConnectError("wrong_step", 409)


def _check_attempts(settings: Settings, flow: ConnectFlow) -> None:
    if flow.attempts >= settings.connect_max_attempts_per_step:
        _finish(flow, ConnectFlowStatus.FAILED, "too_many_attempts")
        raise ConnectError("too_many_attempts", 429)


def _wrong_secret(settings: Settings, flow: ConnectFlow, code: str) -> ConnectError:
    flow.attempts += 1
    left = settings.connect_max_attempts_per_step - flow.attempts
    if left <= 0:
        _finish(flow, ConnectFlowStatus.FAILED, "too_many_attempts")
        return ConnectError("too_many_attempts", 429)
    err = ConnectError(code, 400)
    err.retry_after = None
    return err


async def _codes_last_hour(session: AsyncSession, user_id: int) -> int:
    since = _now() - timedelta(hours=1)
    return int(
        (
            await session.execute(
                select(func.coalesce(func.sum(ConnectFlow.codes_sent), 0)).where(
                    ConnectFlow.user_id == user_id, ConnectFlow.updated_at >= since
                )
            )
        ).scalar_one()
    )


async def get_status(session: AsyncSession, token: str, telegram_user_id: int) -> dict[str, Any]:
    flow, _ = await _load(session, token, telegram_user_id)
    return view(flow)


async def submit_phone(
    session: AsyncSession,
    settings: Settings,
    keyring: Keyring,
    gateway: LoginGateway,
    token: str,
    telegram_user_id: int,
    raw_phone: str,
) -> dict[str, Any]:
    flow, user = await _load(session, token, telegram_user_id)
    _require(flow, ConnectFlowStatus.ISSUED, ConnectFlowStatus.CODE_SENT)
    phone = normalize_phone(raw_phone)
    if await _codes_last_hour(session, user.id) >= settings.connect_max_codes_per_hour:
        raise ConnectError("too_many_codes", 429, retry_after=3600)
    try:
        pending = await gateway.send_code(phone)
    except TelegramGatewayError as exc:
        raise _gateway_error(flow, exc) from exc
    _store_state(
        keyring,
        flow,
        user,
        {"session": pending.session, "phone": phone, "hash": pending.phone_code_hash, "delivery": pending.delivery.kind},
    )
    flow.status = ConnectFlowStatus.CODE_SENT.value
    flow.codes_sent += 1
    flow.attempts = 0
    flow.phone_masked = mask_phone(phone)
    flow.expires_at = _now() + timedelta(seconds=settings.connect_session_ttl_seconds)
    return view(flow, delivery=asdict(pending.delivery))


async def resend_code(
    session: AsyncSession,
    settings: Settings,
    keyring: Keyring,
    gateway: LoginGateway,
    token: str,
    telegram_user_id: int,
) -> dict[str, Any]:
    flow, user = await _load(session, token, telegram_user_id)
    _require(flow, ConnectFlowStatus.CODE_SENT)
    if await _codes_last_hour(session, user.id) >= settings.connect_max_codes_per_hour:
        raise ConnectError("too_many_codes", 429, retry_after=3600)
    state = _load_state(keyring, flow, user)
    try:
        pending = await gateway.resend_code(state["session"], state["phone"], state["hash"])
    except TelegramGatewayError as exc:
        if exc.code == "code_expired":
            flow.status = ConnectFlowStatus.ISSUED.value
            flow.encrypted_state = flow.encryption_key_id = None
            raise ConnectError("code_expired") from exc
        raise _gateway_error(flow, exc) from exc
    state.update(session=pending.session, hash=pending.phone_code_hash, delivery=pending.delivery.kind)
    _store_state(keyring, flow, user, state)
    flow.codes_sent += 1
    flow.attempts = 0
    return view(flow, delivery=asdict(pending.delivery))


async def submit_code(
    session: AsyncSession,
    settings: Settings,
    keyring: Keyring,
    gateway: LoginGateway,
    token: str,
    telegram_user_id: int,
    raw_code: str,
) -> dict[str, Any]:
    flow, user = await _load(session, token, telegram_user_id)
    _require(flow, ConnectFlowStatus.CODE_SENT)
    code = re.sub(r"\s", "", raw_code or "")
    if not _CODE_RE.fullmatch(code):
        raise ConnectError("code_invalid")
    _check_attempts(settings, flow)
    state = _load_state(keyring, flow, user)
    try:
        result = await gateway.sign_in(
            state["session"], state["phone"], state["hash"], code, email=state.get("delivery") == "email"
        )
    except TelegramGatewayError as exc:
        if exc.code == "code_invalid":
            raise _wrong_secret(settings, flow, "code_invalid") from exc
        if exc.code == "code_expired":
            flow.status = ConnectFlowStatus.ISSUED.value
            flow.encrypted_state = flow.encryption_key_id = None
            raise ConnectError("code_expired") from exc
        raise _gateway_error(flow, exc) from exc
    if result.password_required:
        state["session"] = result.session
        _store_state(keyring, flow, user, state)
        flow.status = ConnectFlowStatus.PASSWORD_REQUIRED.value
        flow.attempts = 0
        return view(flow)
    return await _complete(session, settings, keyring, gateway, flow, user, result)


async def password_params(
    session: AsyncSession, keyring: Keyring, gateway: LoginGateway, token: str, telegram_user_id: int
) -> dict[str, Any]:
    flow, user = await _load(session, token, telegram_user_id)
    _require(flow, ConnectFlowStatus.PASSWORD_REQUIRED)
    state = _load_state(keyring, flow, user)
    try:
        challenge = await gateway.password_challenge(state["session"])
    except TelegramGatewayError as exc:
        raise _gateway_error(flow, exc) from exc
    return view(
        flow,
        password={
            "srp_id": str(challenge.srp_id),
            "srp_b": challenge.srp_b.hex(),
            "g": challenge.g,
            "p": challenge.p.hex(),
            "salt1": challenge.salt1.hex(),
            "salt2": challenge.salt2.hex(),
            "hint": challenge.hint,
        },
    )


def _parse_hex(value: str, length: int) -> bytes:
    try:
        data = bytes.fromhex(value)
    except ValueError as exc:
        raise ConnectError("password_invalid") from exc
    if len(data) != length:
        raise ConnectError("password_invalid")
    return data


async def submit_password(
    session: AsyncSession,
    settings: Settings,
    keyring: Keyring,
    gateway: LoginGateway,
    token: str,
    telegram_user_id: int,
    *,
    srp_id: str,
    a_hex: str,
    m1_hex: str,
) -> dict[str, Any]:
    flow, user = await _load(session, token, telegram_user_id)
    _require(flow, ConnectFlowStatus.PASSWORD_REQUIRED)
    if not re.fullmatch(r"-?\d{1,20}", srp_id or ""):
        raise ConnectError("password_challenge_expired", 409)
    a, m1 = _parse_hex(a_hex, 256), _parse_hex(m1_hex, 32)
    _check_attempts(settings, flow)
    state = _load_state(keyring, flow, user)
    try:
        result = await gateway.check_password(state["session"], int(srp_id), a, m1)
    except TelegramGatewayError as exc:
        if exc.code == "password_invalid":
            raise _wrong_secret(settings, flow, "password_invalid") from exc
        if exc.code == "password_challenge_expired":
            raise ConnectError("password_challenge_expired", 409) from exc
        raise _gateway_error(flow, exc) from exc
    return await _complete(session, settings, keyring, gateway, flow, user, result)


async def _complete(
    session: AsyncSession,
    settings: Settings,
    keyring: Keyring,
    gateway: LoginGateway,
    flow: ConnectFlow,
    user: User,
    result: SignInResult,
) -> dict[str, Any]:
    profile = result.profile
    assert profile is not None
    if settings.connect_require_same_account and profile.account_id != user.telegram_user_id:
        try:
            await gateway.log_out(result.session)
        except TelegramGatewayError as exc:
            log.warning("could not log out mismatched account", extra={"code": exc.code})
        _finish(flow, ConnectFlowStatus.FAILED, "account_mismatch")
        raise ConnectError("account_mismatch", 403)

    replaced_sessions: list[str] = []
    previous = await active_connections_for_update(session, user.id)
    for conn in previous:
        try:
            replaced_sessions.append(open_session(keyring, conn))
        except SessionInvalidError:
            pass
        conn.connection_status = ConnectionStatus.REVOKED.value
        conn.encrypted_session = None
        conn.encryption_key_id = None
        conn.revoked_at = func.now()
        conn.revocation_reason = "replaced"
    await revoke_channel_authorizations(session, [conn.id for conn in previous])
    await session.flush()

    sealed = seal_session(keyring, result.session, user_id=user.id, account_id=profile.account_id)
    now = _now()
    session.add(
        TelegramConnection(
            user_id=user.id,
            encrypted_session=sealed.blob,
            encryption_key_id=sealed.key_id,
            connection_status=ConnectionStatus.ACTIVE.value,
            connected_account_id=profile.account_id,
            account_username=profile.username,
            account_display_name=profile.display_name[:255],
            account_phone_masked=flow.phone_masked,
            connected_at=now,
            last_validated_at=now,
        )
    )
    _finish(flow, ConnectFlowStatus.COMPLETED)
    await session.flush()

    for old in replaced_sessions:
        try:
            await gateway.log_out(old)
        except TelegramGatewayError as exc:
            log.warning("could not log out replaced session", extra={"code": exc.code})

    return view(flow, account={"display_name": profile.display_name, "username": profile.username})


async def cancel(session: AsyncSession, token: str, telegram_user_id: int) -> dict[str, Any]:
    flow, _ = await _load(session, token, telegram_user_id)
    if flow.status in OPEN_CONNECT_FLOW_STATUSES:
        _finish(flow, ConnectFlowStatus.CANCELLED, "user_cancelled")
    return view(flow)
