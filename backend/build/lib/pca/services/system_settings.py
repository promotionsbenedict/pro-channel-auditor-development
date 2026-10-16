from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pca.config import Settings
from pca.db.models import SystemSetting
from pca.services.subscription import SubscriptionConfig

SUB_CHANNEL_ID = "subscription.channel_id"
SUB_CHANNEL_URL = "subscription.channel_url"
SUB_CHANNEL_USERNAME = "subscription.channel_username"
SUB_ENFORCED = "subscription.enforced"
SUB_GATE_TEXT = "subscription.gate_text"
SUPPORT_CONTACT = "support.contact"

_SUBSCRIPTION_KEYS = (SUB_CHANNEL_ID, SUB_CHANNEL_URL, SUB_CHANNEL_USERNAME, SUB_ENFORCED, SUB_GATE_TEXT)


async def get_settings_map(session: AsyncSession, keys: tuple[str, ...]) -> dict[str, Any]:
    rows = (await session.execute(select(SystemSetting).where(SystemSetting.key.in_(keys)))).scalars()
    return {row.key: row.value for row in rows}


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(SystemSetting, key)
    return default if row is None else row.value


async def set_setting(session: AsyncSession, key: str, value: Any, *, actor_user_id: int | None) -> None:
    stmt = pg_insert(SystemSetting).values(key=key, value=value, updated_by_user_id=actor_user_id)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": stmt.excluded.value, "updated_by_user_id": actor_user_id, "updated_at": func.now()},
        )
    )


async def load_subscription_config(session: AsyncSession, settings: Settings) -> SubscriptionConfig:
    overrides = await get_settings_map(session, _SUBSCRIPTION_KEYS)
    channel_id = overrides.get(SUB_CHANNEL_ID, settings.required_channel_id)
    return SubscriptionConfig(
        enforced=bool(overrides.get(SUB_ENFORCED, settings.subscription_enforced)),
        channel_id=int(channel_id) if channel_id is not None else None,
        channel_url=str(overrides.get(SUB_CHANNEL_URL, settings.required_channel_url)),
        channel_username=str(overrides.get(SUB_CHANNEL_USERNAME, settings.required_channel_username)),
        gate_text=overrides.get(SUB_GATE_TEXT),
    )


async def get_support_contact(session: AsyncSession, settings: Settings) -> str:
    return str(await get_setting(session, SUPPORT_CONTACT, settings.support_contact))
