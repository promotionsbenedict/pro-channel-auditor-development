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
    public_base_url: str = "http://localhost:8000"

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
        if self.environment == "production":
            problems = []
            if not self.bot_token.get_secret_value():
                problems.append("PCA_BOT_TOKEN is required")
            if self.required_channel_id is None and self.subscription_enforced:
                problems.append("PCA_REQUIRED_CHANNEL_ID (numeric chat id) is required when subscription is enforced")
            if not self.admin_telegram_ids:
                problems.append("PCA_ADMIN_TELEGRAM_IDS must list at least one numeric admin id")
            if not self.public_base_url.startswith("https://"):
                problems.append("PCA_PUBLIC_BASE_URL must use https:// in production")
            if problems:
                raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
