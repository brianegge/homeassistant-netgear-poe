"""Tests for the protocol helpers in api.py."""

from __future__ import annotations

from base64 import b64decode, b64encode
from unittest.mock import AsyncMock

import pytest

from custom_components.netgear_poe.api import (
    NetgearAuthError,
    NetgearError,
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


async def test_probe_answers_json() -> None:
    """The CGI answering any JSON to an unauth status read means present."""
    api = NetgearPoeApi("host", "pw")
    api._request = AsyncMock(return_value={"data": {"status": "fail"}})

    assert await api.async_probe() is True
    api._request.assert_awaited_once_with("get.cgi", "home_loginStatus")


async def test_probe_no_cgi() -> None:
    """A host without the JSON CGI (404 / HTML answer) probes False."""
    api = NetgearPoeApi("host", "pw")
    api._request = AsyncMock(side_effect=NetgearError("Non-JSON response"))

    assert await api.async_probe() is False


async def test_get_info_parses_firmware() -> None:
    """sys_info maps sysName, the model lang key and fwVer."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(
        return_value={
            "data": {
                "sysName": "boiler-switch",
                "sysProduct": "lang('sys','txtModelDescpGS728TPv2')",
                "fwVer": "6.0.8.15",
            }
        }
    )

    info = await api.async_get_info()
    assert info.name == "boiler-switch"
    assert info.model == "GS728TPv2"
    assert info.firmware == "6.0.8.15"


async def test_get_info_without_firmware() -> None:
    """A sys_info response without fwVer yields an empty firmware string."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(
        return_value={"data": {"sysName": "sw", "sysProduct": "GS728TPv2"}}
    )

    info = await api.async_get_info()
    assert info.firmware == ""


async def test_get_info_gs310tp_field_spellings() -> None:
    """The GS310TP names the same facts differently; both spellings work.

    It sends txtSwVer (not fwVer), sysObjectOid (not sysObjectID) and a plain
    txtVerModelName — reading only the GS728TPv2 names left firmware and
    sysObjectID blank, which also broke LATEST_FIRMWARE lookups.
    """
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(
        return_value={
            "data": {
                "sysName": "office-switch",
                "sysProduct": "lang('login','txtModelDescpGS310TP')",
                "txtVerModelName": "GS310TP",
                "txtSwVer": "1.0.1.2",
                "sysObjectOid": "1.3.6.1.4.1.4526.100.4.53",
            }
        }
    )

    info = await api.async_get_info()
    assert info.model == "GS310TP"
    assert info.firmware == "1.0.1.2"
    assert info.sys_object_id == "1.3.6.1.4.1.4526.100.4.53"


PORT_PORT_RESPONSE = {
    "data": {
        "ports": [
            {
                "ifindex": 1,
                "portName": "GE1",
                "descp": "old-name",
                "adminStatus": 1,
                "adminSpeed": "lang('common','lblAuto')",
                "adminDuplex": "lang('common','lblAuto')",
                "adminFlowCtrl": "lang('common','lblDisabled')",
            },
            {"ifindex": 2, "descp": ""},
        ]
    }
}


async def test_set_port_name_posts_edit_form() -> None:
    """The edit form carries the encoded name and echoes link settings."""
    api = NetgearPoeApi("host", "pw")
    calls: list[tuple[str, str, str | None]] = []

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        calls.append((cgi, cmd, body))
        if cmd == "port_port":
            return PORT_PORT_RESPONSE
        return {"status": "ok"}

    api._authed_request = AsyncMock(side_effect=fake_request)
    await api.async_set_port_name(1, "garage cam")

    cgi, cmd, body = calls[-1]
    assert (cgi, cmd) == ("set.cgi", "port_portEdit")
    assert body is not None
    assert "portList=GE1" in body
    assert "descp=garage%20cam" in body
    assert "adminStatus=on" in body
    assert "adminSpeed=auto" in body
    assert "adminDuplex=auto" in body
    assert "adminFlowCtrl=disable" in body
    # The cached alias updates immediately for the next poll.
    assert api._port_names[1] == "garage cam"


async def test_set_port_name_refuses_sparse_row() -> None:
    """A row without link settings aborts the rename rather than guessing.

    The edit form echoes every link setting back, so defaulting a missing
    one ("on"/"auto"/"disable") would rewrite the port's configuration.
    """
    api = NetgearPoeApi("host", "pw")
    calls: list[tuple[str, str, str | None]] = []

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        calls.append((cgi, cmd, body))
        if cmd == "port_port":
            return PORT_PORT_RESPONSE
        return {"status": "ok"}

    api._authed_request = AsyncMock(side_effect=fake_request)
    api._port_names = {2: "stale"}

    with pytest.raises(NetgearError, match="adminStatus missing"):
        await api.async_set_port_name(2, "")

    # Nothing was written and the cached alias is untouched.
    assert all(cmd != "port_portEdit" for _cgi, cmd, _body in calls)
    assert api._port_names[2] == "stale"


async def test_set_port_name_refuses_numeric_speed() -> None:
    """A numeric speed code has no safe form label, so the rename aborts."""
    api = NetgearPoeApi("host", "pw")
    row = dict(PORT_PORT_RESPONSE["data"]["ports"][0], adminSpeed="3")
    api._authed_request = AsyncMock(return_value={"data": {"ports": [row]}})

    with pytest.raises(NetgearError, match="adminSpeed='3'"):
        await api.async_set_port_name(1, "cam")


async def test_set_port_name_unknown_port() -> None:
    """Renaming a port that does not exist raises NetgearError."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(return_value=PORT_PORT_RESPONSE)

    with pytest.raises(NetgearError, match="Port 9 not found"):
        await api.async_set_port_name(9, "x")


async def test_set_port_name_rejected() -> None:
    """A non-ok set status raises NetgearError and keeps the old cache."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        if cmd == "port_port":
            return PORT_PORT_RESPONSE
        return {"status": "err", "msgType": "errInvalidParam"}

    api._authed_request = AsyncMock(side_effect=fake_request)
    api._port_names = {1: "old-name"}

    with pytest.raises(NetgearError, match="Port name set failed"):
        await api.async_set_port_name(1, "new")
    assert api._port_names[1] == "old-name"


# A session token the switch would hand back: 32-char tabid, exponent 10001,
# a modulus (hex) big enough to RSA-encrypt the tabid, and a trailing byte the
# parser drops. A 1024-bit synthetic modulus is plenty for the encrypt path.
_MODULUS = "d" * 256
_TABID = "A" * 32
_SESS = b64encode((_TABID + "10001" + _MODULUS + "Z").encode()).decode()


def test_url_uses_bj4_integrity_param() -> None:
    """The request URL carries the md5 under the modern bj4 parameter."""
    api = NetgearPoeApi("host", "pw")
    url = api._url("get.cgi", "sys_info")
    assert "&bj4=" in url
    assert "&hash=" not in url


async def test_login_new_firmware_authid_handshake() -> None:
    """Newer firmware returns an authId that is posted back for the session."""
    api = NetgearPoeApi("host", "pw")
    calls: list[tuple[str, str, str | None]] = []

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        calls.append((cgi, cmd, body))
        if cmd == "home_loginAuth":
            return {"status": "ok", "authId": "deadbeef"}
        if cmd == "home_loginStatus":
            assert cgi == "set.cgi"
            assert "authId=deadbeef" in (body or "")
            return {"data": {"status": "ok", "sess": _SESS}}
        raise AssertionError(cmd)

    api._request = AsyncMock(side_effect=fake_request)
    await api.async_login()

    assert api._xsid_header is not None
    # The session came from the POSTed authId, not a bare GET.
    assert calls[-1][:2] == ("set.cgi", "home_loginStatus")
    assert calls[-1][2] is not None


async def test_login_old_firmware_get_status() -> None:
    """Older firmware grants the session on the GET status poll (no authId)."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        if cmd == "home_loginAuth":
            return {"status": "ok", "msgType": "save_success"}
        if cmd == "home_loginStatus":
            assert cgi == "get.cgi"
            assert body is None
            return {"data": {"status": "ok", "sess": _SESS}}
        raise AssertionError(cmd)

    api._request = AsyncMock(side_effect=fake_request)
    await api.async_login()

    assert api._xsid_header is not None


async def test_login_falls_back_to_hash_param() -> None:
    """A 400 on bj4 makes the driver retry and cache the hash spelling."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        if cmd == "home_loginAuth":
            if api._hash_param == "bj4":
                err = NetgearError("400")
                err.status = 400
                raise err
            return {"status": "ok", "msgType": "save_success"}
        if cmd == "home_loginStatus":
            return {"data": {"status": "ok", "sess": _SESS}}
        raise AssertionError(cmd)

    api._request = AsyncMock(side_effect=fake_request)
    await api.async_login()

    assert api._hash_param == "hash"
    assert api._xsid_header is not None


async def test_login_does_not_flip_param_on_non_400() -> None:
    """A transient (non-400) failure must not change the request parameter.

    Flipping to "hash" on a timeout would leave a modern "bj4" switch stuck on
    the spelling it rejects, so the error is re-raised and bj4 is preserved.
    """
    api = NetgearPoeApi("host", "pw")
    attempts = 0

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        nonlocal attempts
        if cmd == "home_loginAuth":
            attempts += 1
            raise NetgearError("Request home_loginAuth failed: timeout")
        raise AssertionError(cmd)

    api._request = AsyncMock(side_effect=fake_request)
    with pytest.raises(NetgearError):
        await api.async_login()

    assert api._hash_param == "bj4"
    assert attempts == 1  # no alternate-spelling retry


async def test_login_wrong_password_reports_fail() -> None:
    """A fail status from the status poll surfaces as an auth error."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(cgi: str, cmd: str, body: str | None = None) -> dict:
        if cmd == "home_loginAuth":
            return {"status": "ok", "authId": "x"}
        return {"data": {"status": "fail", "failReason": "bad"}}

    api._request = AsyncMock(side_effect=fake_request)
    with pytest.raises(NetgearAuthError):
        await api.async_login()
