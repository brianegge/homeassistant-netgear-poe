"""Tests for the S350 "cheetah" client (GS324TP) in api_base_ui.py.

The fixtures reproduce the real EmWeb cell format: each cell is a hidden
input named ``1.<index>.<count>.v_1_2_<col>`` whose VALUE is the datum, with
the port's ``g<n>`` label always in column 1. Identifiers are fake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.netgear_poe.api import NetgearError
from custom_components.netgear_poe.api_base_ui import NetgearCheetahApi


def _cell(index: int, col: int, value: str, count: int = 24) -> str:
    return (
        f'<TD class="def alt1" id=1_2_{col}>'
        f"<INPUT xid=1_2_{col} TYPE=hidden NAME=1.{index}.{count}.v_1_2_{col} "
        f'VALUE="{value}">{value}</TD>'
    )


def _poe_row(index: int, admin: str, power: str, status: str) -> str:
    # col 1 = g-label, 2 = admin, 15 = output power, 17 = detection status.
    return (
        _cell(index, 1, f"g{index + 1}")
        + _cell(index, 2, admin)
        + _cell(index, 15, power)
        + _cell(index, 17, status)
    )


POE_HTML = (
    "<html><body><table>"
    + _poe_row(0, "Disable", "0.00", "Disabled")
    + _poe_row(1, "Enable", "0.00", "Searching")
    + _poe_row(2, "Enable", "6.10", "Delivering Power")
    + "</table></body></html>"
)


def _port_row(index: int, alias: str, count: int = 26) -> str:
    # col 1 = g-label, 2 = ifAlias.
    return _cell(index, 1, f"g{index + 1}", count) + _cell(index, 2, alias, count)


PORTS_HTML = (
    "<html><body><table>"
    + _port_row(0, "")  # unnamed
    + _port_row(1, "attic switch")
    + _port_row(2, "kitchen cam")
    + "</table></body></html>"
)

POE_CFG_HTML = (
    "<html><body><TD>Consumed</TD><TD><INPUT TYPE=hidden "
    'NAME=x VALUE="24.7">24.7</TD>'
    "<!-- basePoeGlobalConfig_MainPseConsumptionPower --></body></html>"
)

SYSINFO_HTML = """<html><head><TITLE>NETGEAR GS324TP</TITLE></head><body>
<INPUT class="input" type="TEXT" name="sysName" VALUE="test switch">
<table>
<tr><td aid='1_1_1'>Model Name</td><td aid='1_2_1'>GS324TP</td>
<td aid='1_3_1'>B1.0.0.5</td><td aid='1_4_1'>1.0.0.26</td></tr>
</table>
<td class="defaultFont" aid="1_11_1_left">System Object OID</td>
<td class="defaultFont" aid="1_11_1_right" >1.3.6.1.4.1.4526.100.4.55 </td>
</body></html>"""

PAGES = {
    "poeInterfaceConfiguration": POE_HTML,
    "poeConfiguration": POE_CFG_HTML,
    "portsConfiguration": PORTS_HTML,
    "sysInfo": SYSINFO_HTML,
}


def _api(pages: dict[str, str] | None = None) -> NetgearCheetahApi:
    served = PAGES if pages is None else pages
    api = NetgearCheetahApi("host", "pw")

    async def fake_request(path: str, data: dict[str, str] | None = None) -> str:
        for key, html in served.items():
            if key in path:
                return html
        raise NetgearError(f"unexpected path {path}")

    api._request = AsyncMock(side_effect=fake_request)
    return api


def test_login_overrides() -> None:
    """The cheetah login posts to its own page with the submt field only."""
    api = NetgearCheetahApi("host", "pw")
    assert api._login_path == "/base/cheetah_login.html"
    assert api._login_extra_fields == {"submt": "16"}
    assert "login.x" not in api._login_extra_fields


async def test_get_info() -> None:
    """sysInfo maps to name, model, firmware and the Netgear sysObjectID."""
    info = await _api().async_get_info()
    assert info.name == "test switch"
    assert info.model == "GS324TP"
    assert info.firmware == "1.0.0.26"
    assert info.sys_object_id == "1.3.6.1.4.1.4526.100.4.55"


async def test_get_data() -> None:
    """The EmWeb PoE cells map to ports with admin, status and watts."""
    data = await _api().async_get_data()

    assert sorted(data.ports) == [1, 2, 3]
    assert data.ports[1].admin_enabled is False
    assert data.ports[1].detection_status == "disabled"
    assert data.ports[2].detection_status == "searching"
    assert data.ports[3].admin_enabled is True
    assert data.ports[3].detection_status == "delivering"
    assert data.ports[3].power_watts == pytest.approx(6.1)
    # Switch-wide draw comes from the global PoE config page.
    assert data.consumption_watts == pytest.approx(24.7)


async def test_get_data_reads_port_names() -> None:
    """Names come from the ports page; an unnamed port stays empty."""
    data = await _api().async_get_data()
    assert data.ports[1].alias == ""
    assert data.ports[2].alias == "attic switch"
    assert data.ports[3].alias == "kitchen cam"


async def test_get_data_without_ports_raises() -> None:
    """A page with no port cells is an error, not an empty result."""
    api = _api({"poeInterfaceConfiguration": "<html>Access Denied</html>"})
    with pytest.raises(NetgearError, match="No PoE ports"):
        await api.async_get_data()


async def test_set_port_enabled_posts_whole_table_with_one_cell_changed() -> None:
    """Only the target port's admin cell changes; every other field replays.

    The switch echoes the whole table; replaying it verbatim is what keeps a
    single-port write from disturbing the other 23 ports.
    """
    api = _api()
    await api.async_set_port_enabled(3, False)  # g3 is index 2

    path, body = (
        api._request.await_args.args[0],
        api._request.await_args.kwargs["data"],
    )
    assert path == "/poeInterfaceConfiguration.html/a1"
    # Target g3 (index 2) admin cell flipped to Disable...
    assert body["1.2.24.v_1_2_2"] == "Disable"
    assert body["1.2.24.gecb_1_2"] == "on"
    # ...and submit_flag is the Apply op (8), not the form's reload default (0).
    assert body["submit_flag"] == "8"
    # ...while g1 and g2 admin cells ride along at their current values.
    assert body["1.0.24.v_1_2_2"] == "Disable"
    assert body["1.1.24.v_1_2_2"] == "Enable"
    # Read-only status/power cells are echoed too (browser posts all hidden).
    assert body["1.2.24.v_1_2_17"] == "Delivering Power"


async def test_set_port_enabled_enable_sends_enable() -> None:
    """Enabling posts Enable in the target admin cell."""
    api = _api()
    await api.async_set_port_enabled(1, True)
    assert api._request.await_args.kwargs["data"]["1.0.24.v_1_2_2"] == "Enable"


async def test_set_port_enabled_unknown_port_raises() -> None:
    """A port absent from the table is an error, not a silent no-op."""
    api = _api()
    with pytest.raises(NetgearError, match="not found"):
        await api.async_set_port_enabled(99, True)


async def test_set_port_enabled_surfaces_switch_error() -> None:
    """err_flag=1 on the returned page raises with the switch's reason."""
    error_page = POE_HTML.replace(
        "</table>",
        '</table><INPUT type=hidden name="err_flag" VALUE="1">'
        '<INPUT type=hidden name="err_msg" VALUE="Power budget exceeded">',
    )
    api = _api()

    async def fake_request(path: str, data: dict[str, str] | None = None) -> str:
        if path.endswith("/a1"):
            return error_page
        return POE_HTML if "poeInterface" in path else PAGES.get("x", POE_HTML)

    api._request.side_effect = fake_request
    with pytest.raises(NetgearError, match="Power budget exceeded"):
        await api.async_set_port_enabled(1, False)


async def test_power_cycle_toggles_off_then_on() -> None:
    """Power cycle is inherited: off, wait, on — driving set_port_enabled."""
    from unittest.mock import patch

    api = _api()
    calls: list[bool] = []

    async def record(port: int, enabled: bool) -> None:
        calls.append(enabled)

    api.async_set_port_enabled = record
    with patch(
        "custom_components.netgear_poe.api_base_ui.asyncio.sleep", new=AsyncMock()
    ):
        await api.async_power_cycle_port(1)
    assert calls == [False, True]


async def test_set_port_name_still_unsupported() -> None:
    """The ifAlias write is a separate form, not done yet."""
    api = _api()
    with pytest.raises(NetgearError, match="not yet supported"):
        await api.async_set_port_name(1, "x")


async def test_firmware_install_unsupported() -> None:
    """Firmware install is off until the file pages are mapped."""
    assert NetgearCheetahApi.supports_firmware_install is False
