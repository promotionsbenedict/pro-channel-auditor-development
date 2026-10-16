"""Use, validate and revoke a customer's stored MTProto session.

Sessions are decrypted only for the duration of one gateway call and never
logged. A per-connection lock stops two handlers in this process from driving
the same authorization concurrently, which Telegram treats as suspicious.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pca.config import Settings
from pca.db.models import ChannelAuthorization, TelegramConnection
from pca.domain.enums import ChannelAuthorizationStatus, ConnectionStatus
from pca.security.crypto import DecryptionError, Keyring, Purpose, Sealed, stored_session_aad
from pca.telegram.errors import SessionInvalidError, TelegramGatewayError
from pca.telegram.types import AccountGateway, AccountProfile

log = logging.getLogger(__name__)
T = TypeVar("T")

_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def _lock_for(connection_id: int) -> asyncio.Lock:
    lock = _locks.get(connection_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[connection_id] = lock
    return lock


def keyring_from_settings(settings: Settings) -> Keyring:
    return Keyring.parse(settings.session_keys_raw.get_secret_value(), settings.session_active_key_id)


def seal_session(keyring: Keyring, session_string: str, *, user_id: int, account_id: int) -> Sealed:
    return keyring.seal(
        session_string.encode(), purpose=Purpose.STORED_SESSION, aad=stored_session_aad(user_id, account_id)
    )


def open_session(keyring: Keyring, conn: TelegramConnection) -> str:
    if not conn.encrypted_session or not conn.encryption_key_id or conn.connected_account_id is None:
        raise SessionInvalidError("session_invalid", detail="no stored session")
    try:
        plaintext = keyring.open(
            Sealed(conn.encryption_key_id, conn.encrypted_session),
            purpose=Purpose.STORED_SESSION,
            aad=stored_session_aad(conn.user_id, conn.connected_account_id),
        )
    except DecryptionError as exc:
        raise SessionInvalidError("session_unreadable", detail=str(exc)) from exc
    return plaintext.decode()


async def revoke_channel_authorizations(session: AsyncSession, connection_ids: Iterable[int]) -> None:
    ids = list(connection_ids)
    if not ids:
        return
    await session.execute(
        update(ChannelAuthorization)
        .where(
            ChannelAuthorization.telegram_connection_id.in_(ids),
            ChannelAuthorization.authorization_status != ChannelAuthorizationStatus.REVOKED.value,
        )
        .values(authorization_status=ChannelAuthorizationStatus.REVOKED.value, selected_at=None)
    )


def _wipe(conn: TelegramConnection, status: ConnectionStatus, reason: str) -> None:
    conn.connection_status = status.value
    conn.encrypted_session = None
    conn.encryption_key_id = None
    conn.revoked_at = func.now()
    conn.revocation_reason = reason


async def mark_connection_dead(session: AsyncSession, conn: TelegramConnection, reason: str) -> None:
    log.warning("telegram connection invalidated", extra={"connection_id": conn.id, "reason": reason})
    _wipe(conn, ConnectionStatus.INVALID, reason)
    await revoke_channel_authorizations(session, [conn.id])
    await session.flush()


async def run_with_session(
    session: AsyncSession,
    keyring: Keyring,
    conn: TelegramConnection,
    operation: Callable[[str], Awaitable[T]],
) -> T:
    async with _lock_for(conn.id):
        try:
            session_string = open_session(keyring, conn)
            result = await operation(session_string)
        except SessionInvalidError as exc:
            await mark_connection_dead(session, conn, exc.code)
            raise
        if keyring.needs_rotation(Sealed(conn.encryption_key_id or "", conn.encrypted_session or b"")):
            sealed = seal_session(keyring, session_string, user_id=conn.user_id, account_id=conn.connected_account_id)
            conn.encrypted_session, conn.encryption_key_id = sealed.blob, sealed.key_id
        return result


async def validate_connection(
    session: AsyncSession, keyring: Keyring, gateway: AccountGateway, conn: TelegramConnection
) -> AccountProfile:
    profile = await run_with_session(session, keyring, conn, gateway.get_profile)
    if profile.account_id != conn.connected_account_id:
        await mark_connection_dead(session, conn, "account_changed")
        raise SessionInvalidError("session_invalid", detail="account id changed")
    conn.account_username = profile.username
    conn.account_display_name = profile.display_name[:255]
    conn.last_validated_at = func.now()
    await session.flush()
    return profile


async def active_connections_for_update(session: AsyncSession, user_id: int) -> list[TelegramConnection]:
    return list(
        (
            await session.execute(
                select(TelegramConnection)
                .where(
                    TelegramConnection.user_id == user_id,
                    TelegramConnection.connection_status.in_(
                        [ConnectionStatus.ACTIVE.value, ConnectionStatus.PENDING.value]
                    ),
                )
                .with_for_update()
            )
        ).scalars()
    )


async def disconnect(
    session: AsyncSession,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
    user_id: int,
    *,
    reason: str = "user_disconnected",
) -> tuple[int, bool]:
    """Log the stored sessions out on Telegram, then wipe them locally.

    Returns (connections revoked, whether every remote logout succeeded). Local
    revocation always happens, even if Telegram is unreachable, because once the
    ciphertext is gone the service can no longer use that authorization.
    """
    connections = await active_connections_for_update(session, user_id)
    remote_ok = True
    for conn in connections:
        if keyring is None or gateway is None:
            remote_ok = False
        else:
            try:
                async with _lock_for(conn.id):
                    await gateway.log_out(open_session(keyring, conn))
            except SessionInvalidError:
                pass
            except TelegramGatewayError as exc:
                remote_ok = False
                log.warning("remote logout failed", extra={"connection_id": conn.id, "code": exc.code})
        _wipe(conn, ConnectionStatus.REVOKED, reason)
    await revoke_channel_authorizations(session, [conn.id for conn in connections])
    await session.flush()
    return len(connections), remote_ok
