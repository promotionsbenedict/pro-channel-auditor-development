from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration loaded from environment variables (prefix PCA_).

    Runtime-editable values (subscription channel, pricing, packages) live in the
    database and are managed by administrators; the values here are bootstrap defaults.
    """

    model_config = SettingsConfigDict(env_prefix="PCA_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    bot_token: SecretStr = SecretStr("")
    bot_username: str = "prochannelauditor_bot"

    database_url: str = "postgresql+asyncpg://pca:pca@localhost:5432/pca"
    redis_url: str = "redis://localhost:6379/0"

    admin_telegram_ids_raw: str = Field(default="", validation_alias="PCA_ADMIN_TELEGRAM_IDS")

    required_channel_id: int | None = None
    required_channel_username: str = "prochannelauditor"
    required_channel_url: str = "https://t.me/prochannelauditor"
    subscription_enforced: bool = True
    subscription_cache_seconds: int = Field(default=120, ge=0, le=3600)

    support_contact: str = "@prochannelauditor"
    public_base_url: str = ""
    heartbeat_path: str = "/tmp/pca-bot-heartbeat"
    worker_heartbeat_path: str = "/tmp/pca-worker-heartbeat"

    # MTProto application credentials from https://my.telegram.org (Telegram API ToS 2.1: own api_id).
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_device_model: str = "Pro Channel Auditor"
    telegram_system_version: str = "Server"
    telegram_app_version: str = "pca-0.2"

    # Session encryption keyring: "keyid:base64(32 bytes)[,keyid:...]". The active id encrypts; all ids decrypt.
    session_keys_raw: SecretStr = Field(default=SecretStr(""), validation_alias="PCA_SESSION_KEYS")
    session_active_key_id: str = ""

    connect_link_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    connect_session_ttl_seconds: int = Field(default=900, ge=120, le=3600)
    # Only allow connecting the same Telegram account that is talking to the bot.
    connect_require_same_account: bool = True
    connect_max_codes_per_hour: int = Field(default=4, ge=1, le=20)
    connect_max_attempts_per_step: int = Field(default=5, ge=1, le=20)
    connect_max_starts_per_ip_hour: int = Field(default=30, ge=1, le=1000)
    connect_init_data_max_age_seconds: int = Field(default=3600, ge=60, le=86400)

    worker_concurrency: int = Field(default=3, ge=1, le=32)
    worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=60)
    worker_lease_seconds: int = Field(default=120, ge=30, le=3600)
    audit_page_size: int = Field(default=100, ge=10, le=200)
    audit_page_delay_seconds: float = Field(default=1.0, ge=0, le=30)
    audit_max_requests: int = Field(default=50_000, ge=100, le=1_000_000)
    audit_max_duration_seconds: int = Field(default=3 * 3600, ge=60, le=48 * 3600)
    audit_flood_inline_max_seconds: int = Field(default=60, ge=0, le=900)
    audit_max_attempts: int = Field(default=12, ge=1, le=50)
    # Consecutive fetches that add nothing new before the queue is considered exhausted.
    audit_stall_confirmations: int = Field(default=3, ge=1, le=20)
    audit_stall_backoff_seconds: int = Field(default=10, ge=0, le=300)
    # Pending requests change during a scan, so exhaustion only counts as a complete
    # audit when at least this share of the (live) target was actually read.
    audit_min_completion_ratio: float = Field(default=0.95, gt=0, le=1)
    audit_max_active_per_user: int = Field(default=2, ge=1, le=20)
    audit_retention_days: int = Field(default=30, ge=1, le=3650)

    paystack_secret_key: SecretStr = SecretStr("")
    paystack_base_url: str = "https://api.paystack.co"
    paystack_callback_url: str = ""
    paystack_customer_email_domain: str = "customers.prochannelauditor.app"
    payment_pending_expiry_minutes: int = Field(default=120, ge=10, le=7 * 24 * 60)

    @property
    def paystack_mode(self) -> str:
        key = self.paystack_secret_key.get_secret_value()
        return "live" if key.startswith("sk_live_") else "test"

    @property
    def connect_enabled(self) -> bool:
        return bool(
            self.public_base_url
            and self.telegram_api_id
            and self.telegram_api_hash.get_secret_value()
            and self.session_keys_raw.get_secret_value()
            and self.session_active_key_id
        )

    @property
    def admin_telegram_ids(self) -> frozenset[int]:
        ids: set[int] = set()
        for part in self.admin_telegram_ids_raw.replace(" ", "").split(","):
            if not part:
                continue
            if not part.lstrip("-").isdigit():
                raise ValueError(f"PCA_ADMIN_TELEGRAM_IDS must contain numeric Telegram user IDs, got {part!r}")
            ids.add(int(part))
        return frozenset(ids)

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        _ = self.admin_telegram_ids
        if self.session_keys_raw.get_secret_value():
            from pca.security.crypto import Keyring

            Keyring.parse(self.session_keys_raw.get_secret_value(), self.session_active_key_id)
        if self.environment == "production":
            problems = []
            if not self.bot_token.get_secret_value():
                problems.append("PCA_BOT_TOKEN is required")
            if self.subscription_enforced and self.required_channel_id is None and not self.required_channel_username:
                problems.append(
                    "PCA_REQUIRED_CHANNEL_ID or PCA_REQUIRED_CHANNEL_USERNAME is required when subscription is enforced"
                )
            if not self.admin_telegram_ids:
                problems.append("PCA_ADMIN_TELEGRAM_IDS must list at least one numeric admin id")
            # Account connection is optional; when the public URL is set the rest must be complete.
            if self.public_base_url:
                if not self.public_base_url.startswith("https://"):
                    problems.append("PCA_PUBLIC_BASE_URL must use https:// in production")
                if not self.telegram_api_id or not self.telegram_api_hash.get_secret_value():
                    problems.append("PCA_TELEGRAM_API_ID and PCA_TELEGRAM_API_HASH are required for account connection")
                if not self.session_keys_raw.get_secret_value() or not self.session_active_key_id:
                    problems.append("PCA_SESSION_KEYS and PCA_SESSION_ACTIVE_KEY_ID are required for account connection")
            if problems:
                raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
