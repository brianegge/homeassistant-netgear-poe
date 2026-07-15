"""Tests for the classic /base/ web UI client in api_base_ui.py.

The HTML fixtures keep the quirks of the real GS110TP pages: header rows
carry a leading spacer cell, the port-config header is shorter than its data
rows, and an unnamed port renders an empty description cell.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.netgear_poe.api import NetgearAuthError, NetgearError
from custom_components.netgear_poe.api_base_ui import NetgearBaseUiApi


def _cells(*values: str) -> str:
    return "".join(f"<TD CLASS='font10'>{v}</TD>" for v in values)


POE_HEADERS = _cells(
    "&nbsp;",
    "Port",
    "Admin Mode",
    "Max Power",
    "Priority Level",
    "Detection Mode",
    "Class",
    "Timer Schedule",
    "Output Voltage (Volt)",
    "Output Current (mA)",
    "Output Power (Watt)",
    "Power Limit Type",
    "Power Limit (mWatt)",
    "Status",
)


def _poe_row(port: str, admin: str, watts: str, status: str) -> str:
    return (
        "<TR>"
        "<td><input type='checkbox' name='CBox_1'/></td>"
        + _cells(
            port,
            admin,
            "16.2",
            "Low",
            "802.3af 4point Only",
            "0",
            "None",
            "46",
            "97",
            watts,
            "Class",
            "15400",
            status,
        )
        + "</TR>"
    )


# The select row between the header and the data rows must be ignored.
POE_EDIT_ROW = (
    "<TR><td><input type='checkbox'/></td>"
    "<TD><INPUT type='text' name='inputBox_interface1' VALUE=''></TD>"
    "<TD><SELECT name='poeAdminMode'><OPTION value='Blank'>"
    "<OPTION value='Enable'>Enable<OPTION value='Disable'>Disable</SELECT></TD>"
    "</TR>"
)

POE_PORT_HTML = f"""<html><body>
<FORM method="post" ACTION="/base/poe/poe_port_cfg.html">
<TABLE>
<TR>{POE_HEADERS}</TR>
{POE_EDIT_ROW}
{_poe_row("g1", "Enable", "4.500", "Delivering Power")}
{_poe_row("g2", "Disable", "0.000", "Disabled")}
{_poe_row("g3", "Enable", "0.000", "Searching")}
</TABLE>
<INPUT TYPE="hidden" NAME="err_flag" VALUE="0">
<INPUT TYPE="hidden" NAME="err_msg" VALUE="">
</FORM></body></html>"""

POE_PORT_HTML_ERROR = POE_PORT_HTML.replace(
    '<INPUT TYPE="hidden" NAME="err_flag" VALUE="0">',
    '<INPUT TYPE="hidden" NAME="err_flag" VALUE="1">',
).replace(
    '<INPUT TYPE="hidden" NAME="err_msg" VALUE="">',
    '<INPUT TYPE="hidden" NAME="err_msg" VALUE="Power limit is out of range">',
)

POE_CFG_HTML = """<html><body><TABLE>
<TR><TD>Power Status</TD><TD>On</TD></TR>
<TR><TD>Nominal Power</TD><TD>46 Watt</TD></TR>
<TR><TD>Consumed Power</TD><TD>36.7 Watt</TD></TR>
</TABLE></body></html>"""

# The real header row stops at "Maximum" while data rows carry three extra
# trailing cells; g1 has no description, which shifts nothing but must parse.
PORT_CFG_HTML = """<html><body>
<FORM method="post" ACTION="/base/system/port/port_cfg_rw.html">
<TABLE>
<TR><TD>&nbsp;</TD><TD>Port</TD><TD>Description</TD><TD>Port Type</TD>
<TD>Admin Mode</TD><TD>Port Speed</TD><TD>Physical Status</TD>
<TD>Link Status</TD><TD>Link Trap</TD><TD>Maximum</TD></TR>
<TR><TD>&nbsp;</TD><TD>g1</TD><TD></TD><TD></TD><TD>Enable</TD><TD>Auto</TD>
<TD></TD><TD>Link Down</TD><TD>Enable</TD><TD>1518</TD>
<TD>AA:BB:CC:DD:EE:FF</TD><TD>1</TD><TD>1</TD></TR>
<TR><TD>&nbsp;</TD><TD>g2</TD><TD>garage cam</TD><TD></TD>
<TD>Enable</TD><TD>Auto</TD><TD>100 Mbps Full Duplex</TD><TD>Link Up</TD>
<TD>Enable</TD><TD>1518</TD><TD>AA:BB:CC:DD:EE:FF</TD><TD>2</TD><TD>2</TD></TR>
</TABLE>
<INPUT TYPE="hidden" NAME="err_flag" VALUE="0">
<INPUT TYPE="hidden" NAME="err_msg" VALUE="">
</FORM></body></html>"""

SYSINFO_HTML = """<html><body>
<INPUT class="input" type="TEXT" name="sysName" SIZE="32" VALUE="boiler-switch">
<INPUT class="input" type="TEXT" name="sysLocation" SIZE="32" VALUE="Basement">
<TABLE>
<TR><TD>Serial Number</TD><TD>ABC1234567890</TD></TR>
<TR><TD>System Object ID</TD><TD>1.3.6.1.4.1.4526.100.4.19</TD></TR>
<TR><TD>Base MAC Address</TD><TD>AA:BB:CC:DD:EE:FB</TD></TR>
</TABLE>
<TABLE>
<TR><TD>&nbsp;</TD><TD>Model Name</TD><TD>Boot Version</TD>
<TD>Software Version</TD></TR>
<TR><TD>GS110TP</TD><TD>B5.1.0.2</TD><TD>5.4.2.33</TD></TR>
</TABLE>
</body></html>"""

LOGIN_PAGE_HTML = """<html><body>
<FORM METHOD="POST" ACTION="/base/main_login.html">
<INPUT type="PASSWORD" name="pwd" MAXLENGTH="20" VALUE="">
<INPUT type="hidden" name="err_flag" VALUE="0">
<INPUT type="hidden" name="err_msg" VALUE="">
</FORM></body></html>"""

PAGES = {
    "poe_port_cfg": POE_PORT_HTML,
    "poe_cfg": POE_CFG_HTML,
    "port_cfg_rw": PORT_CFG_HTML,
    "port/port_cfg": PORT_CFG_HTML,
    "sysInfo": SYSINFO_HTML,
}


def _api(pages: dict[str, str] | None = None) -> NetgearBaseUiApi:
    """Build a client whose _request serves canned pages by path."""
    served = PAGES if pages is None else pages
    api = NetgearBaseUiApi("host", "pw")

    async def fake_request(path: str, data: dict[str, str] | None = None) -> str:
        for key, html in served.items():
            if key in path:
                return html
        raise NetgearError(f"unexpected path {path}")

    api._request = AsyncMock(side_effect=fake_request)
    return api


async def test_get_data() -> None:
    """The PoE table maps to ports with status, watts and consumption."""
    api = _api()
    data = await api.async_get_data()

    assert sorted(data.ports) == [1, 2, 3]
    assert data.ports[1].admin_enabled is True
    assert data.ports[1].detection_status == "delivering"
    assert data.ports[1].power_watts == pytest.approx(4.5)
    assert data.ports[2].admin_enabled is False
    assert data.ports[2].detection_status == "disabled"
    assert data.ports[3].detection_status == "searching"
    # Switch-wide draw comes from poe_cfg.html, not a sum of the ports.
    assert data.consumption_watts == pytest.approx(36.7)


async def test_get_data_reads_port_names() -> None:
    """Port names come from port_cfg.html; an unnamed port stays empty."""
    api = _api()
    data = await api.async_get_data()

    assert data.ports[2].alias == "garage cam"
    assert data.ports[1].alias == ""


async def test_get_data_skips_names_when_snmp_supplies_them() -> None:
    """With SNMP as the name source, port_cfg.html is never fetched."""
    api = _api()
    api.web_port_names_enabled = False
    await api.async_get_data()

    paths = [call.args[0] for call in api._request.await_args_list]
    assert not any("port/port_cfg" in path for path in paths)


async def test_fetch_port_names_retries_then_succeeds() -> None:
    """A transient failure is retried, and the retry's names are kept."""
    api = NetgearBaseUiApi("host", "pw")
    api._request = AsyncMock(side_effect=[NetgearError("transient"), PORT_CFG_HTML])

    with patch(
        "custom_components.netgear_poe.api_base_ui.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        names = await api._async_fetch_port_names(retries=1)

    assert names == {2: "garage cam"}
    # One sleep between the two attempts — never after the last one.
    assert sleep.await_count == 1


async def test_fetch_port_names_raises_after_last_retry() -> None:
    """When every attempt fails, the switch's error surfaces."""
    api = NetgearBaseUiApi("host", "pw")
    api._request = AsyncMock(side_effect=NetgearError("boom"))

    with (
        patch(
            "custom_components.netgear_poe.api_base_ui.asyncio.sleep", new=AsyncMock()
        ),
        pytest.raises(NetgearError, match="boom"),
    ):
        await api._async_fetch_port_names(retries=2)

    assert api._request.await_count == 3


async def test_get_data_keeps_names_when_refresh_fails() -> None:
    """A later name refresh that fails leaves the cached names in place."""
    api = _api({"poe_port_cfg": POE_PORT_HTML, "poe_cfg": POE_CFG_HTML})
    api._port_names = {1: "cached cam"}
    api._poll_count = 20  # due for a refresh; port_cfg is not served -> fails

    data = await api.async_get_data()

    assert data.ports[1].alias == "cached cam"


async def test_get_data_without_ports_raises() -> None:
    """A page with no port rows is an error, not an empty result."""
    api = _api({"poe_port_cfg": "<html><body>Access Denied</body></html>"})
    with pytest.raises(NetgearError, match="No PoE ports"):
        await api.async_get_data()


async def test_get_info() -> None:
    """sysInfo.html maps to name, model, firmware and sysObjectID."""
    api = _api()
    info = await api.async_get_info()

    assert info.name == "boiler-switch"
    assert info.model == "GS110TP"
    assert info.firmware == "5.4.2.33"
    assert info.sys_object_id == "1.3.6.1.4.1.4526.100.4.19"


async def test_set_port_enabled_posts_form() -> None:
    """Disable targets just that port and leaves other settings alone."""
    api = _api()
    await api.async_set_port_enabled(4, False)

    path, body = api._request.await_args.args[0], api._request.await_args.kwargs["data"]
    assert path == "/base/poe/poe_port_cfg.html"
    assert body["selectedPorts"] == "g4;"
    assert body["poeAdminMode"] == "Disable"
    assert body["submt"] == "16"
    assert body["multiple_ports"] == "0"
    # "Blank" is the switch's leave-unchanged sentinel on this form.
    assert body["poePriority"] == "Blank"
    assert body["poeDetectionMode"] == "Blank"
    assert body["poePowerLimitType"] == "Blank"

    await api.async_set_port_enabled(4, True)
    assert api._request.await_args.kwargs["data"]["poeAdminMode"] == "Enable"


async def test_set_port_enabled_omits_foreign_fields() -> None:
    """The PoE form has no "cncel"; sending one makes the switch answer 400."""
    api = _api()
    await api.async_set_port_enabled(1, True)
    assert "cncel" not in api._request.await_args.kwargs["data"]


async def test_set_port_enabled_surfaces_error() -> None:
    """err_flag=1 on the returned page raises with the switch's reason."""
    api = _api({"poe_port_cfg": POE_PORT_HTML_ERROR})
    with pytest.raises(NetgearError, match="out of range"):
        await api.async_set_port_enabled(1, False)


async def test_set_port_name_posts_form() -> None:
    """The name lands in portDesc without disturbing speed or admin mode."""
    api = _api()
    await api.async_set_port_name(2, "garage cam")

    path, body = api._request.await_args.args[0], api._request.await_args.kwargs["data"]
    assert path == "/base/system/port/port_cfg_rw.html"
    assert body["selectedPorts"] == "g2;"
    assert body["portDesc"] == "garage cam"
    # "None" is the leave-unchanged sentinel on the port-config form; an
    # empty frameSize keeps the configured MTU.
    assert body["adminMode"] == "None"
    assert body["physicalMode"] == "None"
    assert body["linkTrap"] == "None"
    assert body["frameSize"] == ""
    # This form, unlike the PoE one, does define "cncel".
    assert body["cncel"] == ""
    # ...and defines none of the PoE page's scheduling fields.
    assert "click_sched" not in body
    assert api._port_names[2] == "garage cam"


async def test_set_port_name_empty_clears_cached_name() -> None:
    """Clearing a description drops it from the cached names."""
    api = _api()
    api._port_names = {2: "old"}
    await api.async_set_port_name(2, "")
    assert 2 not in api._port_names


async def test_power_cycle_retries_restore() -> None:
    """A transient failure restoring power is retried, not left off."""
    api = NetgearBaseUiApi("host", "pw")
    calls: list[bool] = []

    async def fake_set(port: int, enabled: bool) -> None:
        calls.append(enabled)
        if enabled and calls.count(True) == 1:
            raise NetgearError("transient")

    api.async_set_port_enabled = fake_set
    with patch(
        "custom_components.netgear_poe.api_base_ui.asyncio.sleep", new=AsyncMock()
    ):
        await api.async_power_cycle_port(1)

    assert calls == [False, True, True]


async def test_trap_registration_unsupported() -> None:
    """This UI has no trap registration; it must fail loudly, not silently."""
    api = NetgearBaseUiApi("host", "pw")
    with pytest.raises(NetgearError, match="not supported"):
        await api.async_ensure_trap_destination("192.168.1.5", "public")


LOGIN_FAILED_HTML = LOGIN_PAGE_HTML.replace(
    'name="err_msg" VALUE=""', 'name="err_msg" VALUE="Login failure"'
)


def _login_session(with_sid: bool) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.text = AsyncMock(
        return_value="<html>ok</html>" if with_sid else LOGIN_FAILED_HTML
    )
    # aiohttp's session.post() returns a context manager, and the client uses
    # it as one so the response is released on every path.
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    session.cookie_jar = [MagicMock(key="SID")] if with_sid else []
    return session


async def test_login_success_sets_session() -> None:
    """A SID cookie means the password was accepted."""
    api = NetgearBaseUiApi("host", "pw", session=_login_session(True))
    await api.async_login()
    assert api._logged_in is True


async def test_login_releases_response_when_body_read_fails() -> None:
    """A failure part-way through the body still releases the connection.

    raise_for_status() releases on a non-2xx itself, but a mid-body error
    would otherwise strand a connection in this long-lived session.
    """
    session = _login_session(True)
    session.post.return_value.__aenter__.return_value.text = AsyncMock(
        side_effect=aiohttp.ClientPayloadError("truncated")
    )
    api = NetgearBaseUiApi("host", "pw", session=session)

    with pytest.raises(NetgearError, match="Login request failed"):
        await api.async_login()

    session.post.return_value.__aexit__.assert_awaited_once()


async def test_login_rejected_without_sid_cookie() -> None:
    """No SID cookie means a bad password; the page's reason is surfaced."""
    api = NetgearBaseUiApi("host", "pw", session=_login_session(False))
    with pytest.raises(NetgearAuthError, match="Login failure"):
        await api.async_login()


async def test_request_relogins_when_session_expired() -> None:
    """An expired session serves the login page, triggering one re-login."""
    api = NetgearBaseUiApi("host", "pw")
    api._logged_in = True
    api.async_login = AsyncMock(side_effect=lambda: setattr(api, "_logged_in", True))
    api._attempt_request = AsyncMock(side_effect=[None, POE_PORT_HTML])

    html = await api._request("/base/poe/poe_port_cfg.html")

    assert html == POE_PORT_HTML
    api.async_login.assert_awaited_once()


async def test_request_gives_up_after_relogin() -> None:
    """A session rejected even after re-login raises rather than looping."""
    api = NetgearBaseUiApi("host", "pw")
    api.async_login = AsyncMock(side_effect=lambda: setattr(api, "_logged_in", True))
    api._attempt_request = AsyncMock(return_value=None)

    with pytest.raises(NetgearAuthError, match="Session rejected"):
        await api._request("/base/poe/poe_port_cfg.html")
