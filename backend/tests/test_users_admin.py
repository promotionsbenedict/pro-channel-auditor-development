from __future__ import annotations

import pytest
from sqlalchemy import func, select

from pca.config import Settings
from pca.db.models import AdminAction, CreditWallet
from pca.domain.enums import UserRole, UserStatus
from pca.services import admin as admin_service
from pca.services import pricing
from pca.services import users as user_service

from .conftest import make_user


async def test_register_is_idempotent_and_creates_one_wallet(sessionmaker):
    first = await make_user(sessionmaker, 100)
    second = await make_user(sessionmaker, 100)
    assert first.id == second.id
    async with sessionmaker() as s:
        count = await s.scalar(select(func.count()).select_from(CreditWallet).where(CreditWallet.user_id == first.id))
    assert count == 1


async def test_admin_bootstrap_uses_numeric_ids_only(sessionmaker):
    async with sessionmaker() as s, s.begin():
        impostor = await user_service.register_user(
            s, telegram_user_id=200, username="prochannelauditor", bootstrap_admin_ids=[999]
        )
        real = await user_service.register_user(s, telegram_user_id=999, username=None, bootstrap_admin_ids=[999])
    assert impostor.role == UserRole.CUSTOMER and not impostor.is_admin
    assert real.role == UserRole.ADMIN


async def test_existing_admin_role_is_preserved_on_reregistration(sessionmaker):
    await make_user(sessionmaker, 300, admin=True)
    again = await make_user(sessionmaker, 300)
    assert again.is_admin


async def test_non_admin_cannot_use_admin_services(sessionmaker):
    customer = await make_user(sessionmaker, 400)
    async with sessionmaker() as s:
        with pytest.raises(admin_service.AdminRequiredError):
            await admin_service.platform_stats(s, customer)
        with pytest.raises(admin_service.AdminRequiredError):
            await admin_service.grant_credits(s, customer, telegram_user_id=400, credits=10, reason="x")


async def test_admin_grant_credits_and_logs_action(sessionmaker):
    admin = await make_user(sessionmaker, 500, admin=True)
    target = await make_user(sessionmaker, 501)
    async with sessionmaker() as s, s.begin():
        result = await admin_service.grant_credits(s, admin, telegram_user_id=501, credits=25, reason="goodwill")
    assert result.created
    async with sessionmaker() as s:
        wallet = await s.scalar(select(CreditWallet).where(CreditWallet.user_id == target.id))
        actions = (await s.execute(select(AdminAction))).scalars().all()
    assert wallet.available_credits == 25
    assert [a.action for a in actions] == ["credits.grant"]


async def test_admin_grant_requires_reason_and_known_user(sessionmaker):
    admin = await make_user(sessionmaker, 600, admin=True)
    async with sessionmaker() as s:
        with pytest.raises(admin_service.AdminOperationError):
            await admin_service.grant_credits(s, admin, telegram_user_id=601, credits=5, reason="x")
        with pytest.raises(admin_service.AdminOperationError):
            await admin_service.grant_credits(s, admin, telegram_user_id=600, credits=5, reason="  ")


async def test_suspend_and_reactivate(sessionmaker):
    admin = await make_user(sessionmaker, 700, admin=True)
    await make_user(sessionmaker, 701)
    async with sessionmaker() as s, s.begin():
        user = await admin_service.set_user_status(s, admin, telegram_user_id=701, status=UserStatus.SUSPENDED)
    assert user.status == UserStatus.SUSPENDED
    async with sessionmaker() as s, s.begin():
        user = await admin_service.set_user_status(s, admin, telegram_user_id=701, status=UserStatus.ACTIVE)
    assert user.status == UserStatus.ACTIVE


async def test_pricing_quotes_by_tier(sessionmaker):
    async with sessionmaker() as s, s.begin():
        await pricing.upsert_tier(s, minimum=1, maximum=100, credits=1)
        await pricing.upsert_tier(s, minimum=101, maximum=1000, credits=5)
    async with sessionmaker() as s:
        assert (await pricing.quote(s, 50)).credits == 1
        assert (await pricing.quote(s, 101)).credits == 5
        with pytest.raises(pricing.NoTierForSizeError):
            await pricing.quote(s, 5000)
        with pytest.raises(pricing.PricingError):
            await pricing.quote(s, 0)


def test_production_settings_validation():
    with pytest.raises(ValueError, match="PCA_BOT_TOKEN"):
        Settings(environment="production", _env_file=None)
    ok = Settings(
        environment="production",
        bot_token="123:abc",
        required_channel_id=-100,
        PCA_ADMIN_TELEGRAM_IDS="1, 2",
        public_base_url="https://example.com",
        _env_file=None,
    )
    assert ok.admin_telegram_ids == frozenset({1, 2})


def test_admin_ids_must_be_numeric():
    with pytest.raises(ValueError):
        Settings(PCA_ADMIN_TELEGRAM_IDS="1,@someone", _env_file=None)
