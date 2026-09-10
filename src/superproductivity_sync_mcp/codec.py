"""Wire codec for Super Productivity's file-based sync payloads.

Format written by ``EncryptAndCompressHandlerService`` in the app::

    pf_[C][E]<modelVersion>__<body>

* ``C`` — body is gzip, base64 encoded.
* ``E`` — body is AES-256-GCM, base64 encoded, layout
  ``[salt 16][iv 12][ciphertext + tag]`` with an Argon2id key
  (t=3, m=64 MiB, p=1, 32-byte key). Legacy files use PBKDF2-SHA256
  (1000 iterations, password as salt) and layout ``[iv 12][ciphertext + tag]``.
* Encryption is applied after compression on write; decryption before
  decompression on read.
* Derived keys are cached like the app's session cache (``session-cache.ts``):
  one encrypt salt per password for the life of the process, a bounded decrypt
  cache keyed by a hash of the password and the salt, never the password itself.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "pf_"
END_SEPARATOR = "__"
FILE_MODEL_VERSION = 2
_PREFIX_RE = re.compile(r"^pf_(C)?(E)?(\d+(?:\.\d+)?)__")

SALT_LENGTH = 16
IV_LENGTH = 12
KEY_LENGTH = 32
_ARGON2 = {"time_cost": 3, "memory_cost": 65536, "parallelism": 1}
_MIN_ARGON2_SIZE = SALT_LENGTH + IV_LENGTH + 16
_MIN_LEGACY_SIZE = IV_LENGTH + 16


class CodecError(Exception):
    """Base class for codec failures."""


class InvalidPrefixError(CodecError):
    pass


class DecryptError(CodecError):
    pass


class PasswordRequiredError(CodecError):
    pass


class PlaintextUnexpectedError(CodecError):
    pass


@dataclass(frozen=True)
class PrefixFlags:
    is_compressed: bool
    is_encrypted: bool
    model_version: float | int

    def render(self) -> str:
        mv = self.model_version
        mv_str = str(int(mv)) if float(mv).is_integer() else str(mv)
        return f"{PREFIX}{'C' if self.is_compressed else ''}{'E' if self.is_encrypted else ''}{mv_str}{END_SEPARATOR}"


def parse_prefix(data: str) -> tuple[PrefixFlags, str]:
    m = _PREFIX_RE.match(data)
    if not m:
        head = data[:16].strip()
        shape = "markup" if head.startswith("<") else "json" if head[:1] in "{[" else "other"
        raise InvalidPrefixError(
            f"Sync file does not start with the expected '{PREFIX}' prefix "
            f"(length={len(data)}, head looks like {shape})."
        )
    mv_raw = m.group(3)
    mv: float | int = int(mv_raw) if mv_raw.isdigit() else float(mv_raw)
    return PrefixFlags(bool(m.group(1)), bool(m.group(2)), mv), data[m.end() :]


# --- base64 / gzip ---------------------------------------------------------


def _sanitize_base64(s: str) -> str:
    s = re.sub(r"\s+", "", s)
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    return s + "=" * pad


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(data: str) -> bytes:
    return base64.b64decode(_sanitize_base64(data))


def gzip_to_b64(text: str) -> str:
    return b64encode(gzip.compress(text.encode("utf-8")))


def gunzip_from_b64(data: str) -> str:
    return gzip.decompress(b64decode(data)).decode("utf-8")


# --- encryption ------------------------------------------------------------

KEY_CACHE_MAX = 100  # the app's SESSION_DECRYPT_CACHE_MAX_SIZE
_key_cache: OrderedDict[tuple[bytes, bytes], bytes] = OrderedDict()


def _password_id(password: str) -> bytes:
    """Cache key component for a password that does not keep the password itself in memory."""
    return hashlib.sha256(password.encode("utf-8")).digest()


def derive_key(password: str, salt: bytes) -> bytes:
    cache_key = (_password_id(password), salt)
    key = _key_cache.get(cache_key)
    if key is not None:
        _key_cache.move_to_end(cache_key)
        return key
    key = hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        hash_len=KEY_LENGTH,
        type=Argon2Type.ID,
        **_ARGON2,
    )
    _key_cache[cache_key] = key
    while len(_key_cache) > KEY_CACHE_MAX:
        _key_cache.popitem(last=False)
    return key


_encrypt_salt_cache: dict[bytes, bytes] = {}


def encrypt(text: str, password: str) -> str:
    import os

    pid = _password_id(password)
    salt = _encrypt_salt_cache.get(pid)
    if salt is None:
        salt = os.urandom(SALT_LENGTH)
        _encrypt_salt_cache[pid] = salt
    key = derive_key(password, salt)
    iv = os.urandom(IV_LENGTH)
    ct = AESGCM(key).encrypt(iv, text.encode("utf-8"), None)
    return b64encode(salt + iv + ct)


def _decrypt_legacy(raw: bytes, password: str) -> str:
    pw = password.encode("utf-8")
    key = hashlib.pbkdf2_hmac("sha256", pw, pw, 1000, dklen=KEY_LENGTH)
    iv, ct = raw[:IV_LENGTH], raw[IV_LENGTH:]
    return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")


def decrypt(data: str, password: str) -> str:
    raw = b64decode(data)
    if len(raw) < _MIN_LEGACY_SIZE:
        raise DecryptError("Encrypted data is too short to be valid")
    if len(raw) < _MIN_ARGON2_SIZE:
        try:
            return _decrypt_legacy(raw, password)
        except Exception as e:  # noqa: BLE001
            raise DecryptError("Decryption failed (legacy format) — wrong password?") from e
    salt, iv, ct = (
        raw[:SALT_LENGTH],
        raw[SALT_LENGTH : SALT_LENGTH + IV_LENGTH],
        raw[SALT_LENGTH + IV_LENGTH :],
    )
    try:
        return AESGCM(derive_key(password, salt)).decrypt(iv, ct, None).decode("utf-8")
    except Exception:  # noqa: BLE001
        try:
            return _decrypt_legacy(raw, password)
        except Exception as e:  # noqa: BLE001
            raise DecryptError("Decryption failed — wrong password or corrupt file") from e


# --- top level -------------------------------------------------------------


def json_dumps(obj: Any) -> str:
    """Serialize like ``JSON.stringify``: compact, unicode kept as-is."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode_sync_file(
    text: str, password: str | None, *, encryption_expected: bool | None = None
) -> tuple[PrefixFlags, Any]:
    """Return ``(flags, data)`` for a raw sync file body.

    ``encryption_expected`` mirrors the app's fail-closed guard: when the
    operator configured a password (encryption expected) but the remote file
    is plaintext, refuse to trust it unless explicitly allowed.
    """
    flags, body = parse_prefix(text)
    if encryption_expected and not flags.is_encrypted:
        raise PlaintextUnexpectedError(
            "An encryption password is configured but the remote sync file is not encrypted. "
            "Set SP_ALLOW_PLAINTEXT=true if this is intended."
        )
    if flags.is_encrypted:
        if not password:
            raise PasswordRequiredError(
                "The sync file is encrypted; set SP_ENCRYPTION_PASSWORD to the password "
                "configured in Super Productivity."
            )
        body = decrypt(body, password)
    if flags.is_compressed:
        body = gunzip_from_b64(body)
    try:
        return flags, json.loads(body)
    except json.JSONDecodeError as e:
        raise CodecError(f"Sync file body is not valid JSON: {e}") from e


def encode_sync_file(data: Any, flags: PrefixFlags, password: str | None) -> str:
    body = json_dumps(data)
    if flags.is_compressed:
        body = gzip_to_b64(body)
    if flags.is_encrypted:
        if not password:
            raise PasswordRequiredError("Encryption is enabled but no password is available")
        body = encrypt(body, password)
    return flags.render() + body


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 (content rev, not security)
