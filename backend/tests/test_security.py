from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from pca.security.crypto import DecryptionError, Keyring, Purpose, generate_key
from pca.security.webapp import InitDataError, verify_init_data

BOT_TOKEN = "123456:TEST-token"


def make_keyring(active: str = "k1", **extra: str) -> Keyring:
    keys = {"k1": generate_key(), **extra}
    return Keyring.parse(",".join(f"{k}:{v}" for k, v in keys.items()), active)


def test_seal_round_trip_and_aad_binding():
    ring = make_keyring()
    sealed = ring.seal(b"session-string", purpose=Purpose.STORED_SESSION, aad="user=1")
    assert b"session-string" not in sealed.blob
    assert ring.open(sealed, purpose=Purpose.STORED_SESSION, aad="user=1") == b"session-string"
    with pytest.raises(DecryptionError):
        ring.open(sealed, purpose=Purpose.STORED_SESSION, aad="user=2")
    with pytest.raises(DecryptionError):
        ring.open(sealed, purpose=Purpose.PENDING_LOGIN, aad="user=1")


def test_key_rotation_keeps_old_sessions_readable():
    old_key = generate_key()
    old = Keyring.parse(f"k1:{old_key}", "k1")
    sealed = old.seal(b"s", purpose=Purpose.STORED_SESSION, aad="a")
    rotated = Keyring.parse(f"k1:{old_key},k2:{generate_key()}", "k2")
    assert rotated.open(sealed, purpose=Purpose.STORED_SESSION, aad="a") == b"s"
    assert rotated.needs_rotation(sealed)


@pytest.mark.parametrize("raw", ["k1", "k1:notbase64!!", "k1:" + "QUJD", "bad id:" + generate_key()])
def test_keyring_rejects_malformed_keys(raw):
    with pytest.raises(ValueError):
        Keyring.parse(raw, "k1")


def sign_init_data(fields: dict[str, str], token: str = BOT_TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def init_fields(user_id: int = 42, auth_date: datetime | None = None) -> dict[str, str]:
    when = auth_date or datetime.now(UTC)
    return {
        "auth_date": str(int(when.timestamp())),
        "query_id": "AAE",
        "user": json.dumps({"id": user_id, "first_name": "A", "username": "alice"}, separators=(",", ":")),
    }


def test_valid_init_data_identifies_user():
    identity = verify_init_data(sign_init_data(init_fields()), BOT_TOKEN, max_age_seconds=3600)
    assert identity.telegram_user_id == 42
    assert identity.username == "alice"


def test_init_data_signed_with_other_token_is_rejected():
    with pytest.raises(InitDataError):
        verify_init_data(sign_init_data(init_fields(), token="999:other"), BOT_TOKEN, max_age_seconds=3600)


def test_tampered_init_data_is_rejected():
    data = sign_init_data(init_fields(user_id=42)).replace("%22id%22%3A42", "%22id%22%3A43")
    with pytest.raises(InitDataError):
        verify_init_data(data, BOT_TOKEN, max_age_seconds=3600)


def test_stale_init_data_is_rejected():
    old = sign_init_data(init_fields(auth_date=datetime.now(UTC) - timedelta(hours=2)))
    with pytest.raises(InitDataError, match="expired"):
        verify_init_data(old, BOT_TOKEN, max_age_seconds=3600)


def test_empty_init_data_is_rejected():
    with pytest.raises(InitDataError):
        verify_init_data("", BOT_TOKEN, max_age_seconds=3600)
