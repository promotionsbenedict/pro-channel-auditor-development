"""Telethon-backed implementation of the login and account gateways.

Every call opens a short-lived client from an in-memory StringSession and
disconnects afterwards, so no session file ever touches disk and nothing keeps
a customer's account online between operations. flood_sleep_threshold=0 makes
Telethon surface FloodWait immediately instead of silently sleeping inside an
HTTP request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from telethon import TelegramClient, errors, functions, types, utils
from telethon.password import check_prime_and_good
from telethon.sessions import StringSession

from pca.config import Settings
from pca.telegram.errors import TelegramGatewayError, translate
from pca.telegram.types import (
    AccountProfile,
    ChannelCheck,
    CodeDelivery,
    DiscoveredChannel,
    PasswordChallenge,
    PendingCode,
    SignInResult,
)

_DELIVERY_KINDS = {
    "SentCodeTypeApp": "app",
    "SentCodeTypeSms": "sms",
    "SentCodeTypeSmsWord": "sms",
    "SentCodeTypeSmsPhrase": "sms",
    "SentCodeTypeCall": "call",
    "SentCodeTypeFlashCall": "flash_call",
    "SentCodeTypeMissedCall": "missed_call",
    "SentCodeTypeEmailCode": "email",
    "SentCodeTypeFragmentSms": "fragment",
    "SentCodeTypeFirebaseSms": "firebase",
    "SentCodeTypeSetUpEmailRequired": "email_setup_required",
}
_NEXT_KINDS = {
    "CodeTypeSms": "sms",
    "CodeTypeCall": "call",
    "CodeTypeFlashCall": "flash_call",
    "CodeTypeMissedCall": "missed_call",
    "CodeTypeFragmentSms": "fragment",
}
# Codes we can't receive on the web: a flash call is detected by the phone's dialer, and
# Firebase SMS needs Play Integrity / APNs attestation from an official mobile app.
_WEB_UNUSABLE = {"flash_call", "firebase"}


def _delivery(sent: types.auth.SentCode) -> CodeDelivery:
    sent_type = sent.type
    kind = _DELIVERY_KINDS.get(type(sent_type).__name__, "unknown")
    next_kind = _NEXT_KINDS.get(type(sent.next_type).__name__) if sent.next_type else None
    return CodeDelivery(
        kind=kind,
        length=getattr(sent_type, "length", None),
        next_kind=next_kind,
        timeout=sent.timeout,
        email_pattern=getattr(sent_type, "email_pattern", None),
        fragment_url=getattr(sent_type, "url", None),
    )


def _profile(user: types.User) -> AccountProfile:
    return AccountProfile(
        account_id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username,
        phone=user.phone,
    )


def _rights_dict(rights: types.ChatAdminRights | None) -> dict[str, bool]:
    if rights is None:
        return {}
    return {key: True for key, value in rights.to_dict().items() if key != "_" and value is True}


def _discovered(entity: types.Channel) -> DiscoveredChannel:
    rights = entity.admin_rights
    return DiscoveredChannel(
        telegram_channel_id=utils.get_peer_id(types.PeerChannel(entity.id)),
        access_hash=entity.access_hash or 0,
        title=entity.title or "",
        username=entity.username,
        kind="channel" if entity.broadcast else "supergroup",
        is_creator=bool(entity.creator),
        can_manage_join_requests=bool(entity.creator or (rights and rights.invite_users)),
        admin_rights=_rights_dict(rights),
    )


class TelethonGateway:
    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_api_id or not settings.telegram_api_hash.get_secret_value():
            raise RuntimeError("PCA_TELEGRAM_API_ID and PCA_TELEGRAM_API_HASH must be set")
        self._settings = settings

    def _client(self, session: str) -> TelegramClient:
        s = self._settings
        return TelegramClient(
            StringSession(session or None),
            s.telegram_api_id,
            s.telegram_api_hash.get_secret_value(),
            device_model=s.telegram_device_model,
            system_version=s.telegram_system_version,
            app_version=s.telegram_app_version,
            lang_code="en",
            system_lang_code="en",
            receive_updates=False,
            flood_sleep_threshold=0,
            connection_retries=2,
            request_retries=2,
            retry_delay=1,
            timeout=15,
        )

    @asynccontextmanager
    async def _connected(self, session: str) -> AsyncIterator[TelegramClient]:
        client = self._client(session)
        try:
            await client.connect()
            yield client
        except TelegramGatewayError:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        finally:
            await client.disconnect()

    @staticmethod
    def _save(client: TelegramClient) -> str:
        return StringSession.save(client.session)

    async def _pending(self, client: TelegramClient, sent: object) -> PendingCode:
        if isinstance(sent, types.auth.SentCodePaymentRequired):
            raise TelegramGatewayError("payment_required")
        if isinstance(sent, types.auth.SentCodeSuccess):
            raise TelegramGatewayError("unexpected", detail="SentCodeSuccess")
        assert isinstance(sent, types.auth.SentCode)
        delivery = _delivery(sent)
        if delivery.kind == "email_setup_required":
            raise TelegramGatewayError("email_setup_required")
        return PendingCode(session=self._save(client), phone_code_hash=sent.phone_code_hash, delivery=delivery)

    async def send_code(self, phone: str) -> PendingCode:
        s = self._settings
        async with self._connected("") as client:
            request = functions.auth.SendCodeRequest(
                phone, s.telegram_api_id, s.telegram_api_hash.get_secret_value(), types.CodeSettings()
            )
            try:
                sent = await client(request)
            except errors.AuthRestartError:
                sent = await client(request)
            pending = await self._pending(client, sent)
            if pending.delivery.kind in _WEB_UNUSABLE and pending.delivery.next_kind:
                sent = await client(functions.auth.ResendCodeRequest(phone, pending.phone_code_hash))
                pending = await self._pending(client, sent)
            if pending.delivery.kind in _WEB_UNUSABLE:
                raise TelegramGatewayError("unsupported_delivery", detail=pending.delivery.kind)
            return pending

    async def resend_code(self, session: str, phone: str, phone_code_hash: str) -> PendingCode:
        async with self._connected(session) as client:
            sent = await client(functions.auth.ResendCodeRequest(phone, phone_code_hash))
            pending = await self._pending(client, sent)
            if pending.delivery.kind in _WEB_UNUSABLE:
                raise TelegramGatewayError("unsupported_delivery", detail=pending.delivery.kind)
            return pending

    async def sign_in(
        self, session: str, phone: str, phone_code_hash: str, code: str, *, email: bool
    ) -> SignInResult:
        async with self._connected(session) as client:
            if email:
                request = functions.auth.SignInRequest(
                    phone, phone_code_hash, email_verification=types.EmailVerificationCode(code)
                )
            else:
                request = functions.auth.SignInRequest(phone, phone_code_hash, phone_code=code)
            try:
                result = await client(request)
            except errors.SessionPasswordNeededError:
                return SignInResult(session=self._save(client), password_required=True)
            if isinstance(result, types.auth.AuthorizationSignUpRequired):
                raise TelegramGatewayError("not_registered")
            return SignInResult(session=self._save(client), profile=_profile(result.user))

    async def password_challenge(self, session: str) -> PasswordChallenge:
        async with self._connected(session) as client:
            pwd = await client(functions.account.GetPasswordRequest())
            algo = pwd.current_algo
            if not isinstance(algo, types.PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow):
                raise TelegramGatewayError("unsupported_password", detail=type(algo).__name__)
            # Validate the group parameters here so the browser never does SRP over a bad prime.
            check_prime_and_good(algo.p, algo.g)
            if not pwd.srp_B or pwd.srp_id is None:
                raise TelegramGatewayError("unsupported_password", detail="missing srp")
            return PasswordChallenge(
                srp_id=pwd.srp_id,
                srp_b=pwd.srp_B,
                g=algo.g,
                p=algo.p,
                salt1=algo.salt1,
                salt2=algo.salt2,
                hint=pwd.hint,
            )

    async def check_password(self, session: str, srp_id: int, a: bytes, m1: bytes) -> SignInResult:
        async with self._connected(session) as client:
            result = await client(functions.auth.CheckPasswordRequest(types.InputCheckPasswordSRP(srp_id, a, m1)))
            return SignInResult(session=self._save(client), profile=_profile(result.user))

    async def log_out(self, session: str) -> None:
        if not session:
            return
        try:
            async with self._connected(session) as client:
                await client(functions.auth.LogOutRequest())
        except TelegramGatewayError as exc:
            if exc.code != "session_invalid":
                raise

    async def get_profile(self, session: str) -> AccountProfile:
        async with self._connected(session) as client:
            users = await client(functions.users.GetUsersRequest([types.InputUserSelf()]))
            if not users or not isinstance(users[0], types.User):
                raise TelegramGatewayError("session_invalid")
            return _profile(users[0])

    async def list_admin_channels(self, session: str) -> list[DiscoveredChannel]:
        found: dict[int, DiscoveredChannel] = {}
        async with self._connected(session) as client:
            async for dialog in client.iter_dialogs(limit=None, ignore_migrated=True):
                entity = dialog.entity
                if not isinstance(entity, types.Channel) or entity.left:
                    continue
                if not (entity.creator or entity.admin_rights):
                    continue
                item = _discovered(entity)
                found[item.telegram_channel_id] = item
        return sorted(found.values(), key=lambda c: c.title.lower())

    async def check_channel(self, session: str, telegram_channel_id: int, access_hash: int) -> ChannelCheck:
        bare_id, _ = utils.resolve_id(telegram_channel_id)
        async with self._connected(session) as client:
            full = await client(functions.channels.GetFullChannelRequest(types.InputChannel(bare_id, access_hash)))
            entity = next((c for c in full.chats if isinstance(c, types.Channel) and c.id == bare_id), None)
            if entity is None or entity.left:
                raise TelegramGatewayError("channel_unavailable")
            return ChannelCheck(channel=_discovered(entity), pending_requests=full.full_chat.requests_pending)
