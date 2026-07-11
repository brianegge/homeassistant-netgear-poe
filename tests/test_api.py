"""Tests for the protocol helpers in api.py."""

from __future__ import annotations

from base64 import b64decode

from custom_components.netgear_poe.api import (
    encode_password,
    form_body,
    rsa_encrypt,
)


def test_encode_password_layout() -> None:
    """Password chars sit reversed at every 7th slot with length markers."""
    password = "secret12"
    encoded = encode_password(password)

    assert len(encoded) == 320
    for i, ch in enumerate(reversed(password)):
        assert encoded[6 + 7 * i] == ch
    assert encoded[122] == "0"
    assert encoded[288] == "8"


def test_encode_password_long() -> None:
    """Length markers handle two-digit lengths."""
    encoded = encode_password("a" * 17)
    assert encoded[122] == "1"
    assert encoded[288] == "7"


def test_form_body() -> None:
    """Body is the odd single-key JSON the CGI expects."""
    assert form_body({"pwd": "x", "state": 1}) == '{"_ds=1&pwd=x&state=1&_de=1":{}}'


def test_rsa_encrypt_round_trip() -> None:
    """Ciphertext decrypts to PKCS#1 v1.5 block type 2 with the message."""
    # Small RSA key (p=61?) too small for padding; use 512-bit primes.
    p = 0xF7E75FDC469067FFDC4E847C51F452DF
    q = 0xE1CE0B309FBE4EB03DBC7EAF14A72DE3
    n = p * q
    e = 0x10001
    d = pow(e, -1, (p - 1) * (q - 1))

    message = "0123456789abcdef0123456789abcdef"[:16]
    cipher_b64 = rsa_encrypt(message, format(e, "x"), format(n, "x"))

    k = (n.bit_length() + 7) // 8
    cipher = int.from_bytes(b64decode(cipher_b64), "big")
    block = pow(cipher, d, n).to_bytes(k, "big")

    assert block[0:2] == b"\x00\x02"
    assert block.endswith(b"\x00" + message.encode())
    padding = block[2 : -len(message) - 1]
    assert 0 not in padding
