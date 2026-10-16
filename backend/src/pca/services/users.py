from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import CreditWallet, TelegramConnection, User
from pca.domain.enums import ConnectionStatus, UserRole, UserStatus


async def register_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None,
    bootstrap_admin_ids: Iterable[int] = (),
) -> User:
    """Create or refresh a customer record keyed by immutable Telegram user id.

    Administrators are bootstrapped from configured numeric ids only; usernames
    never grant privileges. Existing database roles are preserved.
    """
    is_bootstrap_admin = telegram_user_id in set(bootstrap_admin_ids)
    insert_stmt = pg_insert(User).values(
        telegram_user_id=telegram_user_id,
        username=username,
        role=UserRole.ADMIN if is_bootstrap_admin else UserRole.CUSTOMER,
        last_active_at=func.now(),
    )
    update_values = {
        "username": insert_stmt.excluded.username,
        "last_active_at": func.now(),
        "updated_at": func.now(),
    }
    if is_bootstrap_admin:
        update_values["role"] = UserRole.ADMIN.value
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[User.telegram_user_id], set_=update_values
    ).returning(User)
    user = (
        await session.execute(select(User).from_statement(stmt).execution_options(populate_existing=True))
    ).scalar_one()

    await session.execute(
        pg_insert(CreditWallet).values(user_id=user.id).on_conflict_do_nothing(index_elements=[CreditWallet.user_id])
    )
    return user


async def bootstrap_admins(session: AsyncSession, admin_ids: Iterable[int]) -> int:
    ids = list(set(admin_ids))
    if not ids:
        return 0
    result = await session.execute(
        update(User)
        .where(User.telegram_user_id.in_(ids), User.role != UserRole.ADMIN.value)
        .values(role=UserRole.ADMIN.value)
    )
    return result.rowcount or 0


async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    ).scalar_one_or_none()


async def get_active_connection(session: AsyncSession, user_id: int) -> TelegramConnection | None:
    return (
        await session.execute(
            select(TelegramConnection).where(
                TelegramConnection.user_id == user_id,
                TelegramConnection.connection_status == ConnectionStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()


async def revoke_connections(session: AsyncSession, user_id: int, reason: str = "user_deleted") -> int:
    """Revoke every stored MTProto session for this user locally and wipe the ciphertext.

    Prefer services.connection.disconnect, which also logs the session out on Telegram.
    """
    from pca.services.connection import revoke_channel_authorizations

    result = await session.execute(
        update(TelegramConnection)
        .where(
            TelegramConnection.user_id == user_id,
            TelegramConnection.connection_status.in_(
                [ConnectionStatus.ACTIVE.value, ConnectionStatus.PENDING.value]
            ),
        )
        .values(
            connection_status=ConnectionStatus.REVOKED.value,
            encrypted_session=None,
            encryption_key_id=None,
            revoked_at=func.now(),
            revocation_reason=reason,
        )
        .returning(TelegramConnection.id)
    )
    ids = list(result.scalars())
    await revoke_channel_authorizations(session, ids)
    return len(ids)


async def request_deletion(session: AsyncSession, user: User) -> None:
    await revoke_connections(session, user.id)
    user.status = UserStatus.DELETION_REQUESTED.value
    user.deletion_requested_at = func.now()
    await session.flush()
    await session.refresh(user)
