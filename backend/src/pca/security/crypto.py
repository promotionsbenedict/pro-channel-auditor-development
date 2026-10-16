"""Authenticated encryption for customer MTProto sessions.

Each master key in the keyring is never used directly: HKDF derives a separate
subkey per purpose, so data encrypted for one purpose (stored sessions) can't
be decrypted as another (in-flight login state). Every ciphertext is bound to
its owner through AES-GCM associated data, so a ciphertext copied onto another
customer's row fails authentication instead of decrypting.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from enum import StrEnum

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_BYTES = 12
FORMAT_VERSION = b"\x01"


class Purpose(StrEnum):
    STORED_SESSION = "pca/stored-session/v1"
    PENDING_LOGIN = "pca/pending-login/v1"


class DecryptionError(Exception):
    """Ciphertext was tampered with, bound to a different owner, or the key is unavailable."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    blob: bytes


class Keyring:
    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError("PCA_SESSION_ACTIVE_KEY_ID does not match any key in PCA_SESSION_KEYS")
        self._keys = keys
        self.active_key_id = active_key_id

    @classmethod
    def parse(cls, raw: str, active_key_id: str) -> Keyring:
        keys: dict[str, bytes] = {}
        for part in raw.replace(" ", "").split(","):
            if not part:
                continue
            key_id, sep, encoded = part.partition(":")
            if not sep or not key_id or not key_id.isalnum() or len(key_id) > 32:
                raise ValueError("PCA_SESSION_KEYS entries must look like keyid:base64key")
            try:
                material = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"PCA_SESSION_KEYS key {key_id!r} is not valid base64") from exc
            if len(material) != 32:
                raise ValueError(f"PCA_SESSION_KEYS key {key_id!r} must decode to exactly 32 bytes")
            keys[key_id] = material
        if not keys:
            raise ValueError("PCA_SESSION_KEYS is empty")
        return cls(keys, active_key_id)

    def _subkey(self, key_id: str, purpose: Purpose) -> AESGCM:
        try:
            master = self._keys[key_id]
        except KeyError as exc:
            raise DecryptionError(f"unknown key id {key_id!r}") from exc
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.value.encode()).derive(master)
        return AESGCM(derived)

    def seal(self, plaintext: bytes, *, purpose: Purpose, aad: str) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._subkey(self.active_key_id, purpose).encrypt(nonce, plaintext, aad.encode())
        return Sealed(self.active_key_id, FORMAT_VERSION + nonce + ciphertext)

    def open(self, sealed: Sealed, *, purpose: Purpose, aad: str) -> bytes:
        blob = sealed.blob
        if len(blob) < 1 + NONCE_BYTES + 16 or blob[:1] != FORMAT_VERSION:
            raise DecryptionError("malformed ciphertext")
        nonce, ciphertext = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        try:
            return self._subkey(sealed.key_id, purpose).decrypt(nonce, ciphertext, aad.encode())
        except InvalidTag as exc:
            raise DecryptionError("authentication failed") from exc

    def needs_rotation(self, sealed: Sealed) -> bool:
        return sealed.key_id != self.active_key_id


def stored_session_aad(user_id: int, telegram_account_id: int) -> str:
    return f"pca:stored-session:user={user_id}:account={telegram_account_id}"


def pending_login_aad(user_id: int, flow_id: str) -> str:
    return f"pca:pending-login:user={user_id}:flow={flow_id}"


def generate_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()
