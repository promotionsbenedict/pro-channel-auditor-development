from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pca.domain.enums import (
    AuditJobStatus,
    ChannelAuthorizationStatus,
    ConnectionStatus,
    PaymentStatus,
    TransactionType,
    UserRole,
    UserStatus,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _one_of(column: str, enum_cls: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


_ACTIVE_AUDIT_SQL = "status IN ('pending', 'queued', 'running', 'waiting_for_telegram')"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (_one_of("role", UserRole), _one_of("status", UserStatus))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), server_default=UserRole.CUSTOMER.value, nullable=False)
    status: Mapped[str] = mapped_column(String(24), server_default=UserStatus.ACTIVE.value, nullable=False)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN and self.status == UserStatus.ACTIVE


class TelegramConnection(Base):
    __tablename__ = "telegram_connections"
    __table_args__ = (
        _one_of("connection_status", ConnectionStatus),
        Index(
            "uq_telegram_connections_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("connection_status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    encrypted_session: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_key_id: Mapped[str | None] = mapped_column(String(32))
    connection_status: Mapped[str] = mapped_column(
        String(16), server_default=ConnectionStatus.PENDING.value, nullable=False
    )
    connected_account_id: Mapped[int | None] = mapped_column(BigInteger)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CreditWallet(Base):
    __tablename__ = "credit_wallets"
    __table_args__ = (
        CheckConstraint("available_credits >= 0", name="available_non_negative"),
        CheckConstraint("reserved_credits >= 0", name="reserved_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=False)
    available_credits: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    reserved_credits: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class CreditPackage(Base):
    __tablename__ = "credit_packages"
    __table_args__ = (
        CheckConstraint("credits > 0", name="credits_positive"),
        CheckConstraint("price_minor > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="NGN", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        _one_of("status", PaymentStatus),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("credits > 0", name="credits_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), server_default="paystack", nullable=False)
    reference: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("credit_packages.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), server_default=PaymentStatus.INITIALIZED.value, nullable=False)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ChannelAuthorization(Base):
    __tablename__ = "channel_authorizations"
    __table_args__ = (
        _one_of("authorization_status", ChannelAuthorizationStatus),
        UniqueConstraint(
            "user_id", "channel_id", "telegram_connection_id", name="uq_channel_authorizations_owner"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True, nullable=False)
    telegram_connection_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False
    )
    authorization_status: Mapped[str] = mapped_column(String(24), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class AuditJob(Base):
    __tablename__ = "audit_jobs"
    __table_args__ = (
        _one_of("status", AuditJobStatus),
        CheckConstraint("reserved_credits >= 0", name="reserved_non_negative"),
        Index(
            "uq_audit_jobs_one_active_per_channel",
            "user_id",
            "channel_id",
            unique=True,
            postgresql_where=text(_ACTIVE_AUDIT_SQL),
        ),
        Index("ix_audit_jobs_claimable", "status", "not_before"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="RESTRICT"), nullable=False)
    telegram_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_connections.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), server_default=AuditJobStatus.PENDING.value, nullable=False)
    expected_requests: Mapped[int | None] = mapped_column(Integer)
    quoted_credits: Mapped[int | None] = mapped_column(Integer)
    reserved_credits: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    charged_credits: Mapped[int | None] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    progress_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AuditCheckpoint(Base):
    __tablename__ = "audit_checkpoints"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_job_id: Mapped[int] = mapped_column(
        ForeignKey("audit_jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    pagination_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    progress_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class AuditResult(Base):
    __tablename__ = "audit_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_job_id: Mapped[int] = mapped_column(
        ForeignKey("audit_jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    report_path: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = _created_at()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_job_id: Mapped[int] = mapped_column(
        ForeignKey("audit_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class CreditTransaction(Base):
    """Immutable ledger entry. UPDATE/DELETE are blocked by a database trigger.

    amount is the change to the customer's total balance (available + reserved);
    available_delta / reserved_delta record how each wallet bucket moved.
    """

    __tablename__ = "credit_transactions"
    __table_args__ = (
        _one_of("transaction_type", TransactionType),
        CheckConstraint("amount = available_delta + reserved_delta", name="amount_matches_deltas"),
        UniqueConstraint(
            "wallet_id", "transaction_type", "reference", name="uq_credit_transactions_idempotency"
        ),
        Index(
            "uq_credit_transactions_one_reserve_per_audit",
            "audit_job_id",
            unique=True,
            postgresql_where=text("transaction_type = 'audit_reserve'"),
        ),
        Index(
            "uq_credit_transactions_one_finalization_per_audit",
            "audit_job_id",
            unique=True,
            postgresql_where=text("transaction_type IN ('audit_settle', 'audit_release')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("credit_wallets.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    available_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(24), nullable=False)
    reference: Mapped[str] = mapped_column(String(128), nullable=False)
    audit_job_id: Mapped[int | None] = mapped_column(ForeignKey("audit_jobs.id", ondelete="RESTRICT"), index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()


class PricingTier(Base):
    __tablename__ = "pricing_tiers"
    __table_args__ = (
        CheckConstraint("minimum_requests >= 1", name="minimum_positive"),
        CheckConstraint("maximum_requests >= minimum_requests", name="range_ordered"),
        CheckConstraint("credits_required >= 0", name="credits_non_negative"),
        Index("ix_pricing_tiers_active_range", "active", "minimum_requests"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    minimum_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_required: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = _updated_at()
