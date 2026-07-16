"""Tests for the S350 "cheetah" client (GS324TP) in api_base_ui.py.

The fixtures reproduce the real EmWeb cell format: each cell is a hidden
input named ``1.<index>.<count>.v_1_2_<col>`` whose VALUE is the datum, with
the port's ``g<n>`` label always in column 1. Identifiers are fake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


async def test_firmware_install_supported() -> None:
    """This generation can flash (see async_install_firmware)."""
    assert NetgearCheetahApi.supports_firmware_install is True


IMAGE_STATUS_HTML = """<html><body><table>
<TD><INPUT TYPE=hidden NAME=v_4_1 VALUE="1.0.0.26">1.0.0.26</TD>
<!-- basesysImage_sysImage1Version -->
<TD><INPUT TYPE=hidden NAME=v_4_2 VALUE="1.0.0.44">1.0.0.44</TD>
<!-- basesysImage_sysImage2Version -->
<TD><INPUT TYPE=hidden NAME=v_4_3 VALUE="image1">image1</TD>
<!-- basesysImage_sysActiveImageName -->
<TD><INPUT TYPE=hidden NAME=v_4_4 VALUE="image1">image1</TD>
<!-- basesysImage_sysActivatedImageName -->
</table></body></html>"""

UPLOAD_FORM_HTML = (
    "<html><body>"
    "<FORM method=post ENCTYPE='multipart/form-data' "
    'ACTION="/http_file_download.html/a1">'
    # Write-only fields render empty: the switch expects its own JS to fill
    # them back in from xeData before submitting.
    '<INPUT xid=1_1_3 TYPE=hidden NAME=v_1_1_3 VALUE="">'
    '<INPUT xid=1_10_1 TYPE=hidden NAME=v_1_10_1 VALUE="Code">'
    '<INPUT xid=1_2_2 TYPE=hidden NAME=v_1_2_2 VALUE="image1">'
    '<INPUT xid=1_3_1 TYPE=file NAME=".v_1_3_1_handle" VALUE="">'
    '<INPUT xid=1_3_2 TYPE=hidden NAME=v_1_3_2 VALUE=" not in progress">'
    '<INPUT xid=1_3_4 TYPE=hidden NAME=v_1_3_4 VALUE="">'
    '<INPUT xid=1_9_1 TYPE=hidden NAME=v_1_9_1 VALUE="">'
    '<INPUT xid=1_9_2 TYPE=hidden NAME=v_1_9_2 VALUE="">'
    '<INPUT TYPE="hidden" NAME="submit_flag" VALUE="0">'
    '<INPUT TYPE="hidden" NAME="err_msg" VALUE="">'
    "</FORM></body></html>"
)


async def test_get_image_status() -> None:
    """The dual-image page maps to slot versions and active markers."""
    api = _api({"dualImageStatus": IMAGE_STATUS_HTML})
    status = await api.async_get_image_status()

    assert status.versions == {"image1": "1.0.0.26", "image2": "1.0.0.44"}
    assert status.current_active == "image1"
    assert status.next_active == "image1"
    assert status.inactive == "image2"


async def test_get_image_status_unparseable_raises() -> None:
    """A page without the table is an error, never a guessed slot."""
    api = _api({"dualImageStatus": "<html>Access Denied</html>"})
    with pytest.raises(NetgearError, match="dual-image"):
        await api.async_get_image_status()


TWO_FORM_HTML = (
    "<html><body>"
    '<FORM method=post ACTION="/http_file_download.html/a0">'
    '<INPUT TYPE="hidden" NAME="applet_port" VALUE="">'
    '<INPUT TYPE="hidden" NAME="applet_slot" VALUE="">'
    "</FORM>"
    + UPLOAD_FORM_HTML.replace("<html><body>", "").replace("</body></html>", "")
    + "</body></html>"
)


def test_cheetah_form_scopes_to_the_target_form() -> None:
    """Fields from the sibling a0 form must not ride along.

    The switch silently drops a submission carrying a field the target form
    doesn't define — it answers cleanly and simply never flashes anything.
    """
    from custom_components.netgear_poe.api_base_ui import (
        _cheetah_form,
        _cheetah_replay,
    )

    body = _cheetah_replay(_cheetah_form(TWO_FORM_HTML, "/a1"), {})
    assert "applet_port" not in body
    assert "applet_slot" not in body
    assert body["v_1_10_1"] == "Code"  # a1's own field is still there


async def test_upload_firmware_posts_the_fields_that_start_the_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload must reproduce the exact field set the switch's UI posts.

    Confirmed against a packet capture of a successful browser upload: File
    Type is the label "Code" (not the enum index), the destination slot goes
    in v_1_9_1 (v_1_2_2 is only a UI helper), the file name is echoed into
    v_1_3_4, and "HTTP Download Start" (v_1_9_2=1) begins the flash. Get any
    of these wrong and the switch takes all 26 MB, answers with a misleading
    error or none at all, and writes nothing — so this asserts the real
    posted body, not the helper.
    """
    api = _api(
        {
            "http_file_download": UPLOAD_FORM_HTML,
            "dualImageStatus": IMAGE_STATUS_HTML,
        }
    )
    session = MagicMock()
    posted = session.post.return_value.__aenter__.return_value
    posted.text = AsyncMock(return_value=UPLOAD_FORM_HTML)  # err_msg is empty
    api._get_session = MagicMock(return_value=session)
    monkeypatch.setattr(
        "custom_components.netgear_poe.api_base_ui.asyncio.sleep", AsyncMock()
    )

    # image2 already holds this version, so the first poll sees it landed.
    await api._async_upload_firmware(b"stk-bytes", "fw.stk", "image2", "1.0.0.44")

    form = session.post.call_args.kwargs["data"]
    values = {options["name"]: value for options, _headers, value in form._fields}
    assert values["v_1_9_2"] == "1"  # HTTP Download Start — the trigger
    assert values["v_1_1_3"] == "HTTP"  # Transfer Mode
    assert values["v_1_10_1"] == "Code"  # File Type as the LABEL, not an index
    assert values["v_1_9_1"] == "image2"  # the real destination slot
    assert values["v_1_2_2"] == "image2"  # UI-helper copy of the slot
    assert values["v_1_3_4"] == "fw.stk"  # file name echoed back
    assert values[".v_1_3_1_handle"] == b"stk-bytes"
    assert values["submit_flag"] == "8"


def test_cheetah_form_missing_raises() -> None:
    """A page without the expected form is an error, not a silent no-op."""
    from custom_components.netgear_poe.api_base_ui import _cheetah_form

    with pytest.raises(NetgearError, match="Could not find"):
        _cheetah_form("<html>Access Denied</html>", "/a1")


def test_cheetah_replay_keeps_the_file_at_its_form_position() -> None:
    """The file part must sit where the form declares it, not at the end.

    The switch's multipart parser is positional.
    """
    from custom_components.netgear_poe.api_base_ui import (
        _cheetah_form,
        _cheetah_replay_ordered,
    )

    fields = _cheetah_replay_ordered(_cheetah_form(TWO_FORM_HTML, "/a1"), {})
    names = [n for n, _ in fields]
    file_at = names.index(".v_1_3_1_handle")
    # Declared after the image-name field and before the transfer flag.
    assert names.index("v_1_2_2") < file_at < names.index("v_1_3_2")
    assert fields[file_at][1] is None  # placeholder for the caller's bytes


def test_cheetah_replay_forces_apply_and_applies_changes() -> None:
    """The replay posts every field, changes the asked-for ones, applies."""
    from custom_components.netgear_poe.api_base_ui import _cheetah_replay

    body = _cheetah_replay(UPLOAD_FORM_HTML, {"v_1_10_1": "0", "v_1_2_2": "image2"})
    # File type posts the enum INDEX; posting the label "Code" is rejected.
    assert body["v_1_10_1"] == "0"
    # The image-name combo is a string, so the slot name posts as-is.
    assert body["v_1_2_2"] == "image2"
    # submit_flag 8 = apply; the form's own 0 only re-renders the page.
    assert body["submit_flag"] == "8"
    # Other fields ride along, and the file part is never a form field.
    assert body["v_1_3_2"] == " not in progress"
    assert ".v_1_3_1_handle" not in body


async def test_transfer_in_progress_reads_the_flag() -> None:
    """The switch reports flash progress only via this field."""
    busy = UPLOAD_FORM_HTML.replace('VALUE=" not in progress"', 'VALUE="In progress"')
    assert await _api({"http_file_download": busy})._async_transfer_in_progress()
    idle = _api({"http_file_download": UPLOAD_FORM_HTML})
    assert not await idle._async_transfer_in_progress()
