from __future__ import annotations

import pytest
from sqlalchemy import select

from pca.config import Settings
from pca.db.models import ConnectFlow, TelegramConnection
from pca.domain.enums import ConnectFlowStatus, ConnectionStatus
from pca.security.crypto import Keyring, generate_key
from pca.services import connect
from pca.services.connection import open_session
from pca.telegram.errors import TelegramGatewayError
from pca.telegram.types import AccountProfile, CodeDelivery, PasswordChallenge, PendingCode, SignInResult

from .conftest import make_user

USER_TID = 5001
PHONE = "+44 7700 900123"


class FakeLogin:
    def __init__(self, *, account_id: int = USER_TID, needs_password: bool = False) -> None:
        self.account_id = account_id
        self.needs_password = needs_password
        self.correct_code = "12345"
        self.logged_out: list[str] = []
        self.codes_sent = 0

    def _done(self, session: str) -> SignInResult:
        return SignInResult(session=session, profile=AccountProfile(account_id=self.account_id, first_name="Ann"))

    async def send_code(self, phone: str) -> PendingCode:
        self.codes_sent += 1
        return PendingCode(session="pending-1", phone_code_hash="hash-1", delivery=CodeDelivery(kind="app", length=5))

    async def resend_code(self, session, phone, phone_code_hash) -> PendingCode:
        return await self.send_code(phone)

    async def sign_in(self, session, phone, phone_code_hash, code, *, email) -> SignInResult:
        assert session == "pending-1" and phone == "447700900123" and phone_code_hash == "hash-1"
        if code != self.correct_code:
            raise TelegramGatewayError("code_invalid")
        if self.needs_password:
            return SignInResult(session="pending-2", password_required=True)
        return self._done("final-session")

    async def password_challenge(self, session) -> PasswordChallenge:
        return PasswordChallenge(srp_id=7, srp_b=b"\x01" * 256, g=3, p=b"\x02" * 256, salt1=b"a", salt2=b"b")

    async def check_password(self, session, srp_id, a, m1) -> SignInResult:
        assert session == "pending-2" and srp_id == 7 and len(a) == 256
        if m1 != b"\x09" * 32:
            raise TelegramGatewayError("password_invalid")
        return self._done("final-session")

    async def log_out(self, session: str) -> None:
        self.logged_out.append(session)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", bot_token="1:x", connect_max_attempts_per_step=3, _env_file=None)


@pytest.fixture
def keyring() -> Keyring:
    return Keyring.parse(f"k1:{generate_key()}", "k1")


async def start(sm, settings) -> tuple[int, str]:
    user = await make_user(sm, USER_TID)
    async with sm() as s, s.begin():
        token = await connect.issue_link(s, settings, user)
    return user.id, token


async def step(sm, fn, *args, **kwargs):
    """Run one request in its own transaction, committing state even when the step raises."""
    async with sm() as s:
        try:
            result = await fn(s, *args, **kwargs)
        except connect.ConnectError:
            await s.commit()
            raise
        await s.commit()
        return result


async def test_full_login_stores_encrypted_session(sessionmaker, settings, keyring):
    user_id, token = await start(sessionmaker, settings)
    gw = FakeLogin()
    sent = await step(sessionmaker, connect.submit_phone, settings, keyring, gw, token, USER_TID, PHONE)
    assert sent["status"] == ConnectFlowStatus.CODE_SENT.value
    done = await step(sessionmaker, connect.submit_code, settings, keyring, gw, token, USER_TID, "12 345")
    assert done["status"] == ConnectFlowStatus.COMPLETED.value

    async with sessionmaker() as s:
        conn = (await s.execute(select(TelegramConnection))).scalar_one()
        flow = (await s.execute(select(ConnectFlow))).scalar_one()
    assert conn.user_id == user_id
    assert conn.connection_status == ConnectionStatus.ACTIVE.value
    assert b"final-session" not in conn.encrypted_session
    assert open_session(keyring, conn) == "final-session"
    assert conn.account_phone_masked and "7700900" not in conn.account_phone_masked
    assert flow.encrypted_state is None

    with pytest.raises(connect.ConnectError) as reused:
        await step(sessionmaker, connect.submit_phone, settings, keyring, gw, token, USER_TID, PHONE)
    assert reused.value.code == "link_used"


async def test_link_cannot_be_used_by_another_telegram_user(sessionmaker, settings, keyring):
    _, token = await start(sessionmaker, settings)
    with pytest.raises(connect.ConnectError) as err:
        await step(sessionmaker, connect.submit_phone, settings, keyring, FakeLogin(), token, 9999, PHONE)
    assert err.value.code == "wrong_user"


async def test_wrong_codes_lock_the_flow(sessionmaker, settings, keyring):
    _, token = await start(sessionmaker, settings)
    gw = FakeLogin()
    await step(sessionmaker, connect.submit_phone, settings, keyring, gw, token, USER_TID, PHONE)
    codes = []
    for _ in range(3):
        with pytest.raises(connect.ConnectError) as err:
            await step(sessionmaker, connect.submit_code, settings, keyring, gw, token, USER_TID, "00000")
        codes.append(err.value.code)
    assert codes == ["code_invalid", "code_invalid", "too_many_attempts"]
    async with sessionmaker() as s:
        flow = (await s.execute(select(ConnectFlow))).scalar_one()
    assert flow.status == ConnectFlowStatus.FAILED.value
    assert flow.encrypted_state is None


async def test_two_step_password_flow(sessionmaker, settings, keyring):
    _, token = await start(sessionmaker, settings)
    gw = FakeLogin(needs_password=True)
    await step(sessionmaker, connect.submit_phone, settings, keyring, gw, token, USER_TID, PHONE)
    pw = await step(sessionmaker, connect.submit_code, settings, keyring, gw, token, USER_TID, "12345")
    assert pw["status"] == ConnectFlowStatus.PASSWORD_REQUIRED.value
    params = await step(sessionmaker, connect.password_params, keyring, gw, token, USER_TID)
    assert params["password"]["srp_id"] == "7"

    a_hex = ("01" * 256)
    with pytest.raises(connect.ConnectError) as err:
        await step(sessionmaker, connect.submit_password, settings, keyring, gw, token, USER_TID,
                   srp_id="7", a_hex=a_hex, m1_hex="00" * 32)
    assert err.value.code == "password_invalid"

    done = await step(sessionmaker, connect.submit_password, settings, keyring, gw, token, USER_TID,
                      srp_id="7", a_hex=a_hex, m1_hex="09" * 32)
    assert done["status"] == ConnectFlowStatus.COMPLETED.value


async def test_signing_into_a_different_account_is_refused_and_logged_out(sessionmaker, settings, keyring):
    _, token = await start(sessionmaker, settings)
    gw = FakeLogin(account_id=USER_TID + 1)
    await step(sessionmaker, connect.submit_phone, settings, keyring, gw, token, USER_TID, PHONE)
    with pytest.raises(connect.ConnectError) as err:
        await step(sessionmaker, connect.submit_code, settings, keyring, gw, token, USER_TID, "12345")
    assert err.value.code == "account_mismatch"
    assert gw.logged_out == ["final-session"]
    async with sessionmaker() as s:
        assert (await s.execute(select(TelegramConnection))).first() is None


async def test_reconnecting_replaces_and_logs_out_previous_session(sessionmaker, settings, keyring):
    user = await make_user(sessionmaker, USER_TID)
    gw = FakeLogin()
    for _ in range(2):
        async with sessionmaker() as s, s.begin():
            token = await connect.issue_link(s, settings, user)
        await step(sessionmaker, connect.submit_phone, settings, keyring, gw, token, USER_TID, PHONE)
        await step(sessionmaker, connect.submit_code, settings, keyring, gw, token, USER_TID, "12345")
    async with sessionmaker() as s:
        rows = (await s.execute(select(TelegramConnection).order_by(TelegramConnection.id))).scalars().all()
    assert [r.connection_status for r in rows] == [ConnectionStatus.REVOKED.value, ConnectionStatus.ACTIVE.value]
    assert rows[0].encrypted_session is None
    assert gw.logged_out == ["final-session"]


async def test_new_link_supersedes_unfinished_one(sessionmaker, settings, keyring):
    user_id, first = await start(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        from pca.db.models import User

        user = await s.get(User, user_id)
        await connect.issue_link(s, settings, user)
    with pytest.raises(connect.ConnectError) as err:
        await step(sessionmaker, connect.submit_phone, settings, keyring, FakeLogin(), first, USER_TID, PHONE)
    assert err.value.code == "link_used"


def test_phone_normalization():
    assert connect.normalize_phone("+44 (7700) 900-123") == "447700900123"
    assert connect.normalize_phone("0044 7700 900123") == "447700900123"
    with pytest.raises(connect.ConnectError):
        connect.normalize_phone("hello")
