"""Tests for the aj4 JSON-CGI client (GS728TPPv3 6.2.x) in api_json_v2.py."""

from __future__ import annotations

from base64 import b64encode
from unittest.mock import MagicMock, patch

import pytest

from custom_components.netgear_poe.api import (
    NetgearAuthError,
    NetgearError,
    NetgearPoeApi,
)
from custom_components.netgear_poe.api_json_v2 import NetgearJsonV2Api
from custom_components.netgear_poe.api_legacy import async_detect_api

# The root page a GS728TPPv3 V6.2.0.36 actually serves: a bootstrap that
# builds login.html?aj4=<ms>&bj4=md5(query) in gotoLogin().
_AJ4_ROOT_BODY = """<html>
<head>
<script type="text/javascript" src="js/fileLoad_v2.js"></script>
<script type="text/javascript">
function gotoLogin()
{
  var fileVer = (new Date().getTime());
  var url = "login.html?aj4="+fileVer;
  url = url + '&bj4=' + md5(url.split('?')[1]);
  window.location.href=url;
}
</script>
</head>
<body onload="gotoLogin();">
</body>
</html>"""

# A session blob as this firmware hands it back: 32-char tabid, exponent
# 10001, and the modulus running to the very end (home.html slices
# substring(37, sess.length) — there is no trailing byte to drop).
_MODULUS = "d" * 255 + "7"
_TABID = "A" * 32
_SESS = b64encode((_TABID + "10001" + _MODULUS).encode()).decode()


def _mock_session(body: str) -> MagicMock:
    resp = MagicMock()
    resp.headers = {}

    async def text(errors: str = "strict") -> str:
        return body

    resp.text = text
    ctx = MagicMock()

    async def aenter(*_: object) -> MagicMock:
        return resp

    async def aexit(*_: object) -> bool:
        return False

    ctx.__aenter__ = aenter
    ctx.__aexit__ = aexit
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


async def test_detect_api_aj4() -> None:
    """The aj4 bootstrap root page selects the redesigned-UI client."""
    session = _mock_session(_AJ4_ROOT_BODY)
    api = await async_detect_api("h", "pw", session=session)
    assert isinstance(api, NetgearJsonV2Api)


async def test_detect_api_plain_json_stays_on_base_client() -> None:
    """A root page without the aj4 marker still selects NetgearPoeApi."""
    session = _mock_session("<html><body>login</body></html>")
    api = await async_detect_api("h", "pw", session=session)
    assert isinstance(api, NetgearPoeApi)
    assert not isinstance(api, NetgearJsonV2Api)


def test_form_body_appends_xsrf() -> None:
    """Bodies without an xsrf field get the live token appended last."""
    api = NetgearJsonV2Api("host", "pw")
    assert api._form_body({"state": 1}) == '{"_ds=1&state=1&xsrf=null&_de=1":{}}'

    api._xsrf = "tok1"
    assert api._form_body({"state": 1}) == '{"_ds=1&state=1&xsrf=tok1&_de=1":{}}'


def test_form_body_replaces_stub_xsrf_in_place() -> None:
    """The base class's xsrf=undefined is replaced without reordering."""
    api = NetgearJsonV2Api("host", "pw")
    api._xsrf = "tok1"
    body = api._form_body({"authId": "a", "xsrf": "undefined"})
    assert body == '{"_ds=1&authId=a&xsrf=tok1&_de=1":{}}'


def test_parse_sess_keeps_whole_modulus() -> None:
    """The modulus runs to the end of the session blob (no [-1] drop)."""
    api = NetgearJsonV2Api("host", "pw")
    tabid, expo, modulus = api._parse_sess(_TABID + "10001" + _MODULUS)
    assert tabid == _TABID
    assert expo == "10001"
    assert modulus == _MODULUS
    # The base class parse would truncate it and corrupt the RSA key.
    assert (
        NetgearPoeApi("host", "pw")._parse_sess(_TABID + "10001" + _MODULUS)[2]
        == _MODULUS[:-1]
    )


async def test_login_flow_and_xsrf_lifecycle() -> None:
    """Login runs the authId handshake, then home_home seeds the xsrf token.

    Later responses carrying an xsrf field rotate the token (the switch
    answers logout/invalidCsrf to a write with a stale one).
    """
    api = NetgearJsonV2Api("host", "pw")
    calls: list[tuple[str, str, str | None]] = []

    async def fake_request(
        self: NetgearPoeApi, cgi: str, cmd: str, body: str | None = None
    ) -> dict:
        calls.append((cgi, cmd, body))
        if cmd == "home_loginAuth":
            # This firmware answers without a status field on success.
            return {"authId": "deadbeef"}
        if cmd == "home_loginStatus":
            assert cgi == "set.cgi"
            assert "authId=deadbeef" in (body or "")
            return {"data": {"status": "ok", "sess": _SESS}}
        if cmd == "home_home":
            assert cgi == "get.cgi"
            return {"data": {"xsrf": "tok1"}}
        if cmd == "poe_portReset":
            return {"status": "ok", "xsrf": "tok2"}
        raise AssertionError(cmd)

    with patch.object(NetgearPoeApi, "_request", fake_request):
        await api.async_login()

        assert api._xsid_header is not None
        assert api._xsrf == "tok1"
        # The login and status bodies went out with the pre-login token.
        assert "xsrf=null" in (calls[0][2] or "")
        assert "xsrf=null" in (calls[1][2] or "")

        # A write carries the live token and picks up the rotated one.
        result = await api._request(
            "set.cgi", "poe_portReset", api._form_body({"state": 1})
        )
        assert result["status"] == "ok"
        assert "xsrf=tok1" in (calls[-1][2] or "")
        assert api._xsrf == "tok2"


async def test_login_rejects_error_status() -> None:
    """status == "error" from home_loginAuth is a rejected password."""
    api = NetgearJsonV2Api("host", "pw")

    async def fake_request(
        self: NetgearPoeApi, cgi: str, cmd: str, body: str | None = None
    ) -> dict:
        return {"status": "error", "msg": "lang('err','errLoginFail')"}

    with (
        patch.object(NetgearPoeApi, "_request", fake_request),
        pytest.raises(NetgearAuthError, match="Login rejected"),
    ):
        await api.async_login()


async def test_login_survives_home_home_failure() -> None:
    """A failed xsrf fetch degrades writes, not the login itself."""
    api = NetgearJsonV2Api("host", "pw")

    async def fake_request(
        self: NetgearPoeApi, cgi: str, cmd: str, body: str | None = None
    ) -> dict:
        if cmd == "home_loginAuth":
            return {"status": "ok", "authId": "deadbeef"}
        if cmd == "home_loginStatus":
            return {"data": {"status": "ok", "sess": _SESS}}
        raise NetgearError("home_home broke")

    with patch.object(NetgearPoeApi, "_request", fake_request):
        await api.async_login()

    assert api._xsid_header is not None
    assert api._xsrf == "null"


async def test_relogin_resets_stale_xsrf() -> None:
    """A re-login must not send the previous session's token."""
    api = NetgearJsonV2Api("host", "pw")
    login_bodies: list[str] = []

    async def fake_request(
        self: NetgearPoeApi, cgi: str, cmd: str, body: str | None = None
    ) -> dict:
        if cmd == "home_loginAuth":
            login_bodies.append(body or "")
            return {"status": "ok", "authId": "deadbeef"}
        if cmd == "home_loginStatus":
            return {"data": {"status": "ok", "sess": _SESS}}
        if cmd == "home_home":
            return {"data": {"xsrf": "tok1"}}
        raise AssertionError(cmd)

    with patch.object(NetgearPoeApi, "_request", fake_request):
        await api.async_login()
        assert api._xsrf == "tok1"
        await api.async_login()

    assert all("xsrf=null" in body for body in login_bodies)


async def test_logout_clears_xsrf() -> None:
    """Logging out drops the session's token along with the XSID header."""
    api = NetgearJsonV2Api("host", "pw")
    api._xsid_header = "xsid"
    api._xsrf = "tok1"

    async def fake_request(
        self: NetgearPoeApi, cgi: str, cmd: str, body: str | None = None
    ) -> dict:
        assert cmd == "home_logout"
        assert "xsrf=tok1" in (body or "")
        return {"status": "ok"}

    with patch.object(NetgearPoeApi, "_request", fake_request):
        await api.async_logout()

    assert api._xsid_header is None
    assert api._xsrf == "null"
