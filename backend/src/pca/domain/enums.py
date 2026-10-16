from enum import StrEnum


class UserRole(StrEnum):
    CUSTOMER = "customer"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETION_REQUESTED = "deletion_requested"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    INVALID = "invalid"


class ChannelAuthorizationStatus(StrEnum):
    VERIFIED = "verified"
    INSUFFICIENT_RIGHTS = "insufficient_rights"
    REVOKED = "revoked"


class ConnectFlowStatus(StrEnum):
    ISSUED = "issued"
    CODE_SENT = "code_sent"
    PASSWORD_REQUIRED = "password_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


OPEN_CONNECT_FLOW_STATUSES = (
    ConnectFlowStatus.ISSUED.value,
    ConnectFlowStatus.CODE_SENT.value,
    ConnectFlowStatus.PASSWORD_REQUIRED.value,
)


class AuditJobStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_TELEGRAM = "waiting_for_telegram"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_AUDIT_STATUSES: tuple[AuditJobStatus, ...] = (
    AuditJobStatus.PENDING,
    AuditJobStatus.QUEUED,
    AuditJobStatus.RUNNING,
    AuditJobStatus.WAITING_FOR_TELEGRAM,
)


class TransactionType(StrEnum):
    PURCHASE = "purchase"
    ADMIN_GRANT = "admin_grant"
    ADMIN_DEBIT = "admin_debit"
    REFUND = "refund"
    AUDIT_RESERVE = "audit_reserve"
    AUDIT_SETTLE = "audit_settle"
    AUDIT_RELEASE = "audit_release"


class PaymentStatus(StrEnum):
    INITIALIZED = "initialized"
    SUCCESS = "success"
    FAILED = "failed"
    ABANDONED = "abandoned"
