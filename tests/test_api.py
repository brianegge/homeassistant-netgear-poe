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
    """Newer firmware answering the unauth home_login read means present."""
    api = NetgearPoeApi("host", "pw")
    api._request = AsyncMock(return_value={"data": {"title": "NETGEAR"}})

    assert await api.async_probe() is True
    api._request.assert_awaited_once_with("get.cgi", "home_login")
    assert api._hash_param == "bj4"


async def test_probe_falls_back_to_old_dialect() -> None:
    """Older firmware 400s home_login but answers home_loginStatus w/ hash."""
    api = NetgearPoeApi("host", "pw")
    api._request = AsyncMock(
        side_effect=[NetgearError("400"), {"data": {"status": "authing"}}]
    )

    assert await api.async_probe() is True
    assert api._request.await_args_list == [
        (("get.cgi", "home_login"),),
        (("get.cgi", "home_loginStatus"),),
    ]
    # The flip is kept so a follow-up login speaks the dialect that worked.
    assert api._hash_param == "hash"


async def test_probe_no_cgi() -> None:
    """A host without the JSON CGI (404 / HTML answers) probes False."""
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


def test_url_hashes_extra_params() -> None:
    """Extra query params sit between cmd and dummy, inside the bj4 hash."""
    from hashlib import md5

    api = NetgearPoeApi("host", "pw")
    url = api._url("get.cgi", "vlan_membership", {"vlan": 21})

    query = url.split("?", 1)[1]
    hashed, bj4 = query.rsplit("&bj4=", 1)
    assert hashed.startswith("cmd=vlan_membership&vlan=21&dummy=")
    assert bj4 == md5(hashed.encode()).hexdigest()


# A 6-port + 2-LAG switch's vlan_membership read: ports first, LAGs after,
# no marker between them (the split comes from home_home's lags list).
_VLAN_READ = {
    "data": {
        "name": "cctv",
        "type": 2,
        "selVid": 21,
        "vlans": [1, 21],
        "ports": [{"state": s} for s in (0, 2, 1, 3, 0, 2, 1, 0)],
    }
}
_HOME_HOME = {"data": {"lags": [{}, {}], "ports": [{}] * 9}}


async def test_get_vlan_membership_splits_ports_and_lags() -> None:
    """Membership reads split the flat state array by home_home's LAG count."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(
        cgi: str, cmd: str, body: str | None = None, params: dict | None = None
    ) -> dict:
        if cmd == "home_home":
            return _HOME_HOME
        assert cmd == "vlan_membership"
        assert params == {"vlan": 21}
        return _VLAN_READ

    api._authed_request = AsyncMock(side_effect=fake_request)
    membership = await api.async_get_vlan_membership(21)

    assert membership.vid == 21
    assert membership.name == "cctv"
    assert membership.ports == {1: 0, 2: 2, 3: 1, 4: 3, 5: 0, 6: 2}
    assert membership.lags == {1: 1, 2: 0}
    assert membership.vlans == [1, 21]

    # The LAG count is cached; a second read must not re-fetch home_home.
    await api.async_get_vlan_membership(21)
    cmds = [c.args[1] for c in api._authed_request.await_args_list]
    assert cmds.count("home_home") == 1


async def test_get_vlan_membership_unknown_vid_raises() -> None:
    """A VID absent from the switch's own VLAN list is an error.

    The switch never rejects a bad VID: this firmware echoes it back with an
    all-zero map, and firmware that ignores the parameter answers for
    another VLAN. Both shapes must raise rather than read as membership.
    """
    api = NetgearPoeApi("host", "pw")

    async def fake_request(
        cgi: str, cmd: str, body: str | None = None, params: dict | None = None
    ) -> dict:
        if cmd == "home_home":
            return _HOME_HOME
        if params == {"vlan": 99}:  # echoed back, but not in vlans
            return {
                "data": {
                    "name": "",
                    "selVid": 99,
                    "vlans": [1, 21],
                    "ports": [{"state": 0}] * 8,
                }
            }
        return _VLAN_READ  # answers for selVid 21 regardless of the ask

    api._authed_request = AsyncMock(side_effect=fake_request)
    with pytest.raises(NetgearError, match="VLAN 99 is not configured"):
        await api.async_get_vlan_membership(99)
    with pytest.raises(NetgearError, match="VLAN 30 is not configured"):
        await api.async_get_vlan_membership(30)


async def test_set_vlan_membership_body() -> None:
    """The write carries the full 0-indexed map, dynamic members as 0."""
    from custom_components.netgear_poe.api import VlanMembership

    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(return_value={"status": "ok"})

    await api.async_set_vlan_membership(
        VlanMembership(
            vid=21,
            name="cctv",
            ports={1: 0, 2: 2, 3: 1, 4: 3},
            lags={1: 3, 2: 2},
        )
    )

    cgi, cmd, body = api._authed_request.await_args.args
    assert (cgi, cmd) == ("set.cgi", "vlan_membership")
    assert body == (
        '{"_ds=1&vlan=21&vlan_0=0&vlan_1=2&vlan_2=1&vlan_3=0'
        '&vlanLag_0=0&vlanLag_1=2&xsrf=undefined&_de=1":{}}'
    )


async def test_set_vlan_membership_error_status() -> None:
    """A non-ok answer to the write is an error."""
    from custom_components.netgear_poe.api import VlanMembership

    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(return_value={"status": "error"})

    with pytest.raises(NetgearError, match="VLAN membership set failed"):
        await api.async_set_vlan_membership(
            VlanMembership(vid=21, name="", ports={1: 0}, lags={})
        )


async def test_set_vlan_port_membership_verifies_write() -> None:
    """The single-port write preserves the map and re-reads to verify."""
    api = NetgearPoeApi("host", "pw")
    states = [0, 2, 1, 3, 0, 2, 1, 0]
    set_bodies: list[str] = []

    async def fake_request(
        cgi: str, cmd: str, body: str | None = None, params: dict | None = None
    ) -> dict:
        if cmd == "home_home":
            return _HOME_HOME
        if cgi == "set.cgi":
            set_bodies.append(body or "")
            states[3] = 0  # the switch applies the change
            return {"status": "ok"}
        return {
            "data": {
                "name": "cctv",
                "selVid": 21,
                "vlans": [1, 21],
                "ports": [{"state": s} for s in states],
            }
        }

    api._authed_request = AsyncMock(side_effect=fake_request)
    after = await api.async_set_vlan_port_membership(21, 4, 0)

    assert after.ports[4] == 0
    # Untouched ports were posted back unchanged (state 3 mapped to 0).
    assert "vlan_1=2&vlan_2=1&vlan_3=0" in set_bodies[0]


async def test_set_vlan_port_membership_mismatch_raises() -> None:
    """A write the switch silently ignores must not report success."""
    api = NetgearPoeApi("host", "pw")

    async def fake_request(
        cgi: str, cmd: str, body: str | None = None, params: dict | None = None
    ) -> dict:
        if cmd == "home_home":
            return _HOME_HOME
        if cgi == "set.cgi":
            return {"status": "ok"}  # claims ok, changes nothing
        return _VLAN_READ

    api._authed_request = AsyncMock(side_effect=fake_request)
    with pytest.raises(NetgearError, match="did not stick"):
        await api.async_set_vlan_port_membership(21, 2, 0)


async def test_set_vlan_port_membership_serializes_concurrent_writes() -> None:
    """Concurrent single-port writes must not clobber each other.

    Two ports are flipped at once against a shared server-side map; the lock
    around the read-modify-write means both changes survive (without it, the
    second write starts from the pre-first-write map and erases port A).
    """
    import asyncio

    api = NetgearPoeApi("host", "pw")
    server = [0, 0, 0, 0]  # states for ports 1-4, mutated by "set"

    async def fake_request(
        cgi: str, cmd: str, body: str | None = None, params: dict | None = None
    ) -> dict:
        if cmd == "home_home":
            return _HOME_HOME
        if cgi == "set.cgi":
            # Apply exactly what the body carries (full-map write).
            for i in range(len(server)):
                token = f"vlan_{i}="
                if token in body:
                    server[i] = int(body.split(token)[1].split("&")[0])
            return {"status": "ok"}
        # A read reflects current server state.
        await asyncio.sleep(0)  # yield, widening the race window
        return {
            "data": {
                "name": "cctv",
                "selVid": 21,
                "vlans": [1, 21],
                "ports": [{"state": s} for s in server],
            }
        }

    api._authed_request = AsyncMock(side_effect=fake_request)
    await asyncio.gather(
        api.async_set_vlan_port_membership(21, 1, 2),
        api.async_set_vlan_port_membership(21, 2, 1),
    )
    assert server[0] == 2  # port 1 change survived
    assert server[1] == 1  # port 2 change survived
