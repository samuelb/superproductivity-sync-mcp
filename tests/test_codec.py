import pytest

from spmcp import codec


def test_prefix_roundtrip_plain():
    flags = codec.PrefixFlags(False, False, 2)
    text = codec.encode_sync_file({"a": 1, "ü": "ö"}, flags, None)
    assert text.startswith("pf_2__{")
    parsed_flags, data = codec.decode_sync_file(text, None)
    assert parsed_flags == flags and data == {"a": 1, "ü": "ö"}


def test_prefix_parse_variants():
    assert codec.parse_prefix("pf_C2__x")[0] == codec.PrefixFlags(True, False, 2)
    assert codec.parse_prefix("pf_E2__x")[0] == codec.PrefixFlags(False, True, 2)
    assert codec.parse_prefix("pf_CE4.5__x")[0] == codec.PrefixFlags(True, True, 4.5)
    assert codec.PrefixFlags(True, True, 2).render() == "pf_CE2__"
    with pytest.raises(codec.InvalidPrefixError):
        codec.parse_prefix('{"version":2}')


def test_gzip_roundtrip():
    flags = codec.PrefixFlags(True, False, 2)
    payload = {"state": {"task": {"ids": list(range(500))}}}
    text = codec.encode_sync_file(payload, flags, None)
    assert text.startswith("pf_C2__")
    assert codec.decode_sync_file(text, None)[1] == payload


def test_encrypt_roundtrip_and_layout():
    flags = codec.PrefixFlags(True, True, 2)
    text = codec.encode_sync_file({"secret": True}, flags, "pw-1")
    assert text.startswith("pf_CE2__")
    body = codec.b64decode(text[len("pf_CE2__") :])
    assert len(body) >= 16 + 12 + 16
    assert codec.decode_sync_file(text, "pw-1")[1] == {"secret": True}
    with pytest.raises(codec.DecryptError):
        codec.decode_sync_file(text, "wrong")
    with pytest.raises(codec.PasswordRequiredError):
        codec.decode_sync_file(text, None)


def test_legacy_pbkdf2_decrypt():
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pw = "legacy"
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), pw.encode(), 1000, dklen=32)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, b'{"old":1}', None)
    text = "pf_E2__" + codec.b64encode(iv + ct)
    assert codec.decode_sync_file(text, pw)[1] == {"old": 1}


def test_plaintext_fail_closed():
    text = codec.encode_sync_file({"a": 1}, codec.PrefixFlags(False, False, 2), None)
    with pytest.raises(codec.PlaintextUnexpectedError):
        codec.decode_sync_file(text, "pw", encryption_expected=True)
    assert codec.decode_sync_file(text, "pw", encryption_expected=False)[1] == {"a": 1}
