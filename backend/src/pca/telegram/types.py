"""Plain data passed between the MTProto gateway and our services.

Services depend only on these types and the Protocols below, so they can be
exercised in tests with an in-memory gateway instead of a live Telegram account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CodeDelivery:
    kind: str
    length: int | None = None
    next_kind: str | None = None
    timeout: int | None = None
    email_pattern: str | None = None
    fragment_url: str | None = None


@dataclass(frozen=True)
class PendingCode:
    session: str
    phone_code_hash: str
    delivery: CodeDelivery


@dataclass(frozen=True)
class AccountProfile:
    account_id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None
    phone: str | None = None

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else str(self.account_id))


@dataclass(frozen=True)
class PasswordChallenge:
    srp_id: int
    srp_b: bytes
    g: int
    p: bytes
    salt1: bytes
    salt2: bytes
    hint: str | None = None


@dataclass(frozen=True)
class SignInResult:
    session: str
    profile: AccountProfile | None = None
    password_required: bool = False


@dataclass(frozen=True)
class DiscoveredChannel:
    telegram_channel_id: int
    access_hash: int
    title: str
    username: str | None
    kind: str
    is_creator: bool
    can_manage_join_requests: bool
    admin_rights: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelCheck:
    channel: DiscoveredChannel
    pending_requests: int | None


class LoginGateway(Protocol):
    async def send_code(self, phone: str) -> PendingCode: ...

    async def resend_code(self, session: str, phone: str, phone_code_hash: str) -> PendingCode: ...

    async def sign_in(
        self, session: str, phone: str, phone_code_hash: str, code: str, *, email: bool
    ) -> SignInResult: ...

    async def password_challenge(self, session: str) -> PasswordChallenge: ...

    async def check_password(self, session: str, srp_id: int, a: bytes, m1: bytes) -> SignInResult: ...

    async def log_out(self, session: str) -> None: ...


class AccountGateway(Protocol):
    async def get_profile(self, session: str) -> AccountProfile: ...

    async def list_admin_channels(self, session: str) -> list[DiscoveredChannel]: ...

    async def check_channel(self, session: str, telegram_channel_id: int, access_hash: int) -> ChannelCheck: ...

    async def log_out(self, session: str) -> None: ...
