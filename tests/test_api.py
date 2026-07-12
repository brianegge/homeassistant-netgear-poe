"""Tests for the protocol helpers in api.py."""

from __future__ import annotations

from base64 import b64decode
from unittest.mock import AsyncMock

from custom_components.netgear_poe.api import (
    NetgearPoeApi,
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
    p = 0xFF23A9D6B9106BAF7DA6F38110E51A6F
    q = 0xF337357993B785BAB1CD1E47EC5ED635
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


async def test_get_data_populates_port_names() -> None:
    """async_get_data merges assigned descriptions from port_port as aliases."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        if cmd == "poe_port":
            return {
                "data": {
                    "ports": [
                        {
                            "state": 1,
                            "status": "lang('poe','txtPortStatusDelivering')",
                            "power": 6500,
                        },
                        {
                            "state": 0,
                            "status": "lang('poe','txtPortStatusSearching')",
                            "power": 0,
                        },
                    ]
                }
            }
        if cmd == "port_port":
            return {
                "data": {
                    "ports": [
                        {"ifindex": 1, "descp": "garage-cam"},
                        {"ifindex": 2, "descp": ""},
                    ]
                }
            }
        raise AssertionError(cmd)

    api._authed_request = AsyncMock(side_effect=fake_request)
    data = await api.async_get_data()

    assert data.ports[1].alias == "garage-cam"
    assert data.ports[2].alias == ""
    assert data.consumption_watts == 6.5
