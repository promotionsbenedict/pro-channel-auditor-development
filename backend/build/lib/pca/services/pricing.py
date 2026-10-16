from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import PricingTier

_PRICING_LOCK_KEY = 7_201_001


class PricingError(Exception):
    pass


class NoTierForSizeError(PricingError):
    def __init__(self, pending_requests: int) -> None:
        super().__init__(f"no active pricing tier covers {pending_requests} requests")
        self.pending_requests = pending_requests


@dataclass(frozen=True)
class Quote:
    pending_requests: int
    credits: int
    tier_id: int


async def list_tiers(session: AsyncSession, *, include_inactive: bool = False) -> list[PricingTier]:
    stmt = select(PricingTier).order_by(PricingTier.minimum_requests, PricingTier.id)
    if not include_inactive:
        stmt = stmt.where(PricingTier.active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def quote(session: AsyncSession, pending_requests: int) -> Quote:
    if pending_requests < 1:
        raise PricingError("nothing to audit: pending request count must be at least 1")
    tier = (
        await session.execute(
            select(PricingTier)
            .where(
                PricingTier.active.is_(True),
                PricingTier.minimum_requests <= pending_requests,
                PricingTier.maximum_requests >= pending_requests,
            )
            .order_by(PricingTier.minimum_requests)
            .limit(1)
        )
    ).scalar_one_or_none()
    if tier is None:
        raise NoTierForSizeError(pending_requests)
    return Quote(pending_requests=pending_requests, credits=tier.credits_required, tier_id=tier.id)


async def upsert_tier(session: AsyncSession, *, minimum: int, maximum: int, credits: int) -> PricingTier:
    """Create a tier, or update credits for an identical active range. Overlaps are rejected."""
    if minimum < 1 or maximum < minimum:
        raise PricingError("range must satisfy 1 <= minimum <= maximum")
    if credits < 0:
        raise PricingError("credits must be non-negative")

    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PRICING_LOCK_KEY})
    active = await list_tiers(session)
    for tier in active:
        if tier.minimum_requests == minimum and tier.maximum_requests == maximum:
            tier.credits_required = credits
            await session.flush()
            return tier
        if minimum <= tier.maximum_requests and tier.minimum_requests <= maximum:
            raise PricingError(
                f"range overlaps active tier #{tier.id} "
                f"({tier.minimum_requests:,}-{tier.maximum_requests:,}); deactivate it first"
            )
    tier = PricingTier(minimum_requests=minimum, maximum_requests=maximum, credits_required=credits)
    session.add(tier)
    await session.flush()
    return tier


async def deactivate_tier(session: AsyncSession, tier_id: int) -> PricingTier | None:
    tier = await session.get(PricingTier, tier_id)
    if tier is None or not tier.active:
        return None
    tier.active = False
    await session.flush()
    return tier
