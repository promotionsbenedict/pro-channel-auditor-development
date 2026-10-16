"""Discover the channels a connected account administers and verify audit permissions.

Audits approve or decline join requests, which Telegram allows for the channel
creator or an admin holding the invite_users right. Rights are re-checked live
against Telegram immediately before any audit starts; the stored value is only
used to render menus.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import Channel, ChannelAuthorization, TelegramConnection
from pca.domain.enums import ChannelAuthorizationStatus
from pca.security.crypto import Keyring
from pca.services.connection import run_with_session
from pca.telegram.errors import TelegramGatewayError
from pca.telegram.types import AccountGateway, DiscoveredChannel


class ChannelPermissionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _status(item: DiscoveredChannel) -> str:
    if item.can_manage_join_requests:
        return ChannelAuthorizationStatus.VERIFIED.value
    return ChannelAuthorizationStatus.INSUFFICIENT_RIGHTS.value


async def _upsert_channel(session: AsyncSession, item: DiscoveredChannel) -> int:
    stmt = pg_insert(Channel).values(
        telegram_channel_id=item.telegram_channel_id,
        title=item.title[:255] or "Untitled",
        username=item.username,
        channel_kind=item.kind,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Channel.telegram_channel_id],
        set_={
            "title": stmt.excluded.title,
            "username": stmt.excluded.username,
            "channel_kind": stmt.excluded.channel_kind,
            "updated_at": func.now(),
        },
    ).returning(Channel.id)
    return (await session.execute(stmt)).scalar_one()


async def refresh_channels(
    session: AsyncSession, keyring: Keyring, gateway: AccountGateway, conn: TelegramConnection
) -> list[tuple[ChannelAuthorization, Channel]]:
    discovered = await run_with_session(session, keyring, conn, gateway.list_admin_channels)
    now = datetime.now(UTC)
    seen: list[int] = []
    for item in discovered:
        channel_id = await _upsert_channel(session, item)
        seen.append(channel_id)
        values = {
            "authorization_status": _status(item),
            "access_hash": item.access_hash,
            "is_creator": item.is_creator,
            "can_manage_join_requests": item.can_manage_join_requests,
            "admin_rights": item.admin_rights,
            "last_verified_at": now,
        }
        stmt = pg_insert(ChannelAuthorization).values(
            user_id=conn.user_id, channel_id=channel_id, telegram_connection_id=conn.id, **values
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    ChannelAuthorization.user_id,
                    ChannelAuthorization.channel_id,
                    ChannelAuthorization.telegram_connection_id,
                ],
                set_={**values, "updated_at": func.now()},
            )
        )
    stale = update(ChannelAuthorization).where(
        ChannelAuthorization.telegram_connection_id == conn.id,
        ChannelAuthorization.authorization_status != ChannelAuthorizationStatus.REVOKED.value,
    )
    if seen:
        stale = stale.where(ChannelAuthorization.channel_id.not_in(seen))
    await session.execute(
        stale.values(authorization_status=ChannelAuthorizationStatus.REVOKED.value, selected_at=None)
    )
    await session.flush()
    return await list_channels(session, conn)


async def list_channels(
    session: AsyncSession, conn: TelegramConnection
) -> list[tuple[ChannelAuthorization, Channel]]:
    rows = await session.execute(
        select(ChannelAuthorization, Channel)
        .join(Channel, Channel.id == ChannelAuthorization.channel_id)
        .where(
            ChannelAuthorization.telegram_connection_id == conn.id,
            ChannelAuthorization.authorization_status != ChannelAuthorizationStatus.REVOKED.value,
        )
        .order_by(func.lower(Channel.title))
        .execution_options(populate_existing=True)
    )
    return [(auth, channel) for auth, channel in rows.all()]


async def get_authorization(
    session: AsyncSession, conn: TelegramConnection, authorization_id: int
) -> tuple[ChannelAuthorization, Channel] | None:
    row = (
        await session.execute(
            select(ChannelAuthorization, Channel)
            .join(Channel, Channel.id == ChannelAuthorization.channel_id)
            .where(
                ChannelAuthorization.id == authorization_id,
                ChannelAuthorization.user_id == conn.user_id,
                ChannelAuthorization.telegram_connection_id == conn.id,
                ChannelAuthorization.authorization_status != ChannelAuthorizationStatus.REVOKED.value,
            )
            .with_for_update(of=ChannelAuthorization)
        )
    ).one_or_none()
    return (row[0], row[1]) if row else None


async def verify_channel(
    session: AsyncSession,
    keyring: Keyring,
    gateway: AccountGateway,
    conn: TelegramConnection,
    auth: ChannelAuthorization,
    channel: Channel,
) -> ChannelAuthorization:
    """Re-check rights live on Telegram and persist the result."""
    access_hash = auth.access_hash or 0
    try:
        check = await run_with_session(
            session, keyring, conn, lambda s: gateway.check_channel(s, channel.telegram_channel_id, access_hash)
        )
    except TelegramGatewayError as exc:
        if exc.code in {"channel_unavailable", "insufficient_rights"}:
            auth.authorization_status = ChannelAuthorizationStatus.REVOKED.value
            auth.selected_at = None
            auth.last_verified_at = datetime.now(UTC)
            await session.flush()
        raise
    item = check.channel
    channel.title = item.title[:255] or channel.title
    channel.username = item.username
    channel.channel_kind = item.kind
    auth.authorization_status = _status(item)
    auth.is_creator = item.is_creator
    auth.can_manage_join_requests = item.can_manage_join_requests
    auth.admin_rights = item.admin_rights
    auth.pending_requests = check.pending_requests
    auth.last_verified_at = datetime.now(UTC)
    await session.flush()
    return auth


async def ensure_can_audit(
    session: AsyncSession,
    keyring: Keyring,
    gateway: AccountGateway,
    conn: TelegramConnection,
    auth: ChannelAuthorization,
    channel: Channel,
) -> ChannelAuthorization:
    """Gate for starting an audit: live-verified right to manage join requests."""
    auth = await verify_channel(session, keyring, gateway, conn, auth, channel)
    if not auth.can_manage_join_requests:
        raise ChannelPermissionError("insufficient_rights")
    auth.selected_at = datetime.now(UTC)
    await session.flush()
    return auth
