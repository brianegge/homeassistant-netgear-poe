"""Tests for the legacy xui client in api_legacy.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.netgear_poe.api import (
    DualImageStatus,
    NetgearError,
    NetgearPoeApi,
    SwitchInfo,
)
from custom_components.netgear_poe.api_base_ui import (
    NetgearBaseUiApi,
    NetgearCheetahApi,
)
from custom_components.netgear_poe.api_legacy import (
    _REBOOT_POLL_ATTEMPTS,
    NetgearLegacyApi,
    _escape_password,
    _parse_xml,
    async_detect_api,
)

POE_LIST_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<DeviceConfiguration>
<PoEPSEInterfaceList type="section">
<Interface>
<interfaceName>g1</interfaceName>
<interfaceID>1</interfaceID>
<adminEnable>1</adminEnable>
<detectionStatus>3</detectionStatus>
<poweredDevice>garage-cam</poweredDevice>
<outputPower>6500</outputPower>
</Interface>
<Interface>
<interfaceName>g2</interfaceName>
<interfaceID>2</interfaceID>
<adminEnable>2</adminEnable>
<detectionStatus>1</detectionStatus>
<poweredDevice></poweredDevice>
<outputPower>0</outputPower>
</Interface>
</PoEPSEInterfaceList>
</DeviceConfiguration>
<ActionStatus>
<statusCode>0</statusCode>
</ActionStatus>
</ResponseData>"""

INFO_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<DeviceConfiguration>
<DeviceBasicInfo type="section">
<deviceName>Boiler Switch</deviceName>
<deviceDescription>16-Port Gigabit PoE Smart Switch</deviceDescription>
<firmwareVersion>6.0.1.16</firmwareVersion>
<systemObjectID>1.3.6.1.4.1.4526.100.4.29</systemObjectID>
</DeviceBasicInfo>
</DeviceConfiguration>
</ResponseData>"""

INFO_XML_NO_FIRMWARE = """<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<DeviceConfiguration>
<DeviceBasicInfo type="section">
<deviceName>Boiler Switch</deviceName>
<deviceDescription>16-Port Gigabit PoE Smart Switch</deviceDescription>
</DeviceBasicInfo>
</DeviceConfiguration>
</ResponseData>"""

SET_OK_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<ActionStatus>
<statusCode>0</statusCode>
<statusString>OK</statusString>
</ActionStatus>
</ResponseData>"""

SET_FAIL_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<ActionStatus>
<statusCode>1</statusCode>
<statusString>pethPsePortAdminEnable is out of range</statusString>
</ActionStatus>
</ResponseData>"""


def test_escape_password() -> None:
    """Only the characters the login page escapes are escaped."""
    assert _escape_password("a b#c%d&e+f") == "a%20b%23c%25d%26e%2Bf"
    assert _escape_password("Plain.Pass123") == "Plain.Pass123"


def test_parse_xml_tolerates_leading_junk() -> None:
    """Responses may carry noise before the XML declaration."""
    root = _parse_xml("\r\n \n<?xml version='1.0'?><a><b>1</b></a>")
    assert root.findtext("b") == "1"


def test_parse_xml_rejects_html() -> None:
    """A non-XML (HTML error) response raises NetgearError."""
    with pytest.raises(NetgearError):
        _parse_xml("<html><body>Access Error</body></html>")


async def test_get_data() -> None:
    """PoE list XML maps to ports with status, watts and consumption."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(return_value=_parse_xml(POE_LIST_XML))

    data = await api.async_get_data()

    assert data.ports[1].admin_enabled is True
    assert data.ports[1].detection_status == "delivering"
    assert data.ports[1].power_watts == pytest.approx(6.5)
    assert data.ports[1].alias == "garage-cam"
    assert data.ports[2].admin_enabled is False
    assert data.ports[2].detection_status == "disabled"
    assert data.consumption_watts == pytest.approx(6.5)


async def test_get_info() -> None:
    """DeviceBasicInfo maps to name, model description and firmware."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(return_value=_parse_xml(INFO_XML))

    info = await api.async_get_info()
    assert info.name == "Boiler Switch"
    assert info.model == "16-Port Gigabit PoE Smart Switch"
    assert info.firmware == "6.0.1.16"
    assert info.sys_object_id == "1.3.6.1.4.1.4526.100.4.29"


async def test_get_info_without_firmware() -> None:
    """A DeviceBasicInfo without a firmware element yields an empty string."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(return_value=_parse_xml(INFO_XML_NO_FIRMWARE))

    info = await api.async_get_info()
    assert info.name == "Boiler Switch"
    assert info.firmware == ""


async def test_set_port_enabled_uses_truthvalue() -> None:
    """Disable posts adminEnable=2 (SNMP TruthValue), not 0."""
    api = NetgearLegacyApi("host", "pw")
    api._if_names = {1: "g1"}
    api._request = AsyncMock(return_value=_parse_xml(SET_OK_XML))

    await api.async_set_port_enabled(1, False)
    body = api._request.call_args.kwargs["body"]
    assert "<interfaceName>g1</interfaceName>" in body
    assert "<adminEnable>2</adminEnable>" in body

    await api.async_set_port_enabled(1, True)
    body = api._request.call_args.kwargs["body"]
    assert "<adminEnable>1</adminEnable>" in body


async def test_set_port_name_escapes_xml() -> None:
    """The name lands in poweredDevice with XML metacharacters escaped."""
    api = NetgearLegacyApi("host", "pw")
    api._if_names = {1: "g1"}
    api._request = AsyncMock(return_value=_parse_xml(SET_OK_XML))

    await api.async_set_port_name(1, "cam & <spare>")
    body = api._request.call_args.kwargs["body"]
    assert "<interfaceName>g1</interfaceName>" in body
    assert "<poweredDevice>cam &amp; &lt;spare&gt;</poweredDevice>" in body


async def test_set_port_name_failure() -> None:
    """A non-zero statusCode surfaces as NetgearError with the reason."""
    api = NetgearLegacyApi("host", "pw")
    api._if_names = {1: "g1"}
    api._request = AsyncMock(return_value=_parse_xml(SET_FAIL_XML))

    with pytest.raises(NetgearError, match="out of range"):
        await api.async_set_port_name(1, "cam")


async def test_set_port_enabled_failure() -> None:
    """A non-zero statusCode surfaces as NetgearError with the reason."""
    api = NetgearLegacyApi("host", "pw")
    api._if_names = {1: "g1"}
    api._request = AsyncMock(return_value=_parse_xml(SET_FAIL_XML))

    with pytest.raises(NetgearError, match="out of range"):
        await api.async_set_port_enabled(1, False)


async def test_request_relogins_when_session_rejected() -> None:
    """A rejected session (302 or malformed header) triggers one re-login."""
    api = NetgearLegacyApi("host", "pw", prefix="csbe1")
    api._cookie = "stale"
    api.async_login = AsyncMock(side_effect=lambda: setattr(api, "_cookie", "new"))
    api._attempt_request = AsyncMock(side_effect=[None, SET_OK_XML])

    root = await api._request("wcd?{X}")
    assert root.findtext(".//statusCode") == "0"
    api.async_login.assert_awaited_once()


async def test_power_cycle_retries_restore() -> None:
    """A transient failure restoring power is retried, not left off."""
    api = NetgearLegacyApi("host", "pw")
    calls: list[bool] = []

    async def fake_set(port: int, enabled: bool) -> None:
        calls.append(enabled)
        if enabled and calls.count(True) == 1:
            raise NetgearError("transient")

    api.async_set_port_enabled = fake_set
    with patch(
        "custom_components.netgear_poe.api_legacy.asyncio.sleep",
        new=AsyncMock(),
    ):
        await api.async_power_cycle_port(1)

    assert calls == [False, True, True]


def _mock_session(location: str | None, body: str = "") -> MagicMock:
    resp = MagicMock()
    resp.headers = {"Location": location} if location else {}
    resp.text = AsyncMock(return_value=body)
    # aiohttp's session.get() returns a context manager, and async_detect_api
    # uses it as one so the probe response is always released.
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    session.close = AsyncMock()
    return session


async def test_detect_api_legacy() -> None:
    """A /csb<hex>/ redirect selects the legacy client with its prefix."""
    session = _mock_session(
        "http://h/csbe123/config/log_off_page.htm"  # NOSONAR — mock redirect
    )
    with patch(
        "custom_components.netgear_poe.api_legacy.aiohttp.ClientSession",
        return_value=session,
    ):
        api = await async_detect_api("h", "pw")
    assert isinstance(api, NetgearLegacyApi)
    assert api._prefix == "csbe123"
    session.close.assert_awaited_once()


@pytest.mark.parametrize(
    "prefix",
    [
        "csb555f027",  # real: hex with letters after "csb" — the common case
        "csbe116353",  # real: happens to be "csbe" + digits
        "csbE116353",  # same, upper case
        "csb0a1b2c3",
    ],
)
async def test_detect_api_legacy_accepts_any_hex_prefix(prefix: str) -> None:
    """The prefix is "csb" + hex, not "csbe" + digits.

    It is recomputed rather than fixed per device, so a switch that answered
    "csbe116353" once can answer "csb555f027" later; matching only the digit
    form silently misroutes the switch to the JSON client.
    """
    session = _mock_session(f"http://h/{prefix}/index.htm")  # NOSONAR — mock
    api = await async_detect_api("h", "pw", session=session)

    assert isinstance(api, NetgearLegacyApi)
    assert api._prefix == prefix


async def test_detect_api_releases_probe_response() -> None:
    """The probe response is released even when its body is never read.

    The legacy branch returns on the redirect header alone, so nothing else
    would hand the connection back to a caller-supplied session.
    """
    session = _mock_session("http://h/csbe123/index.htm")  # NOSONAR — mock
    api = await async_detect_api("h", "pw", session=session)

    assert isinstance(api, NetgearLegacyApi)
    session.get.return_value.__aexit__.assert_awaited_once()
    # A caller-supplied session is the caller's to close.
    session.close.assert_not_called()


async def test_detect_api_base_ui() -> None:
    """A /base/ login form selects the classic web-UI client (GS110TP)."""
    session = _mock_session(None, '<FORM METHOD="POST" ACTION="/base/main_login.html">')
    with patch(
        "custom_components.netgear_poe.api_legacy.aiohttp.ClientSession",
        return_value=session,
    ):
        api = await async_detect_api("h", "pw")
    assert isinstance(api, NetgearBaseUiApi)
    # The cheetah subclass shares the /base/ prefix, so order matters.
    assert not isinstance(api, NetgearCheetahApi)


async def test_detect_api_cheetah() -> None:
    """A /base/cheetah_login.html form selects the S350 client (GS324TP)."""
    session = _mock_session(
        None, '<FORM METHOD="POST" ACTION="/base/cheetah_login.html">'
    )
    with patch(
        "custom_components.netgear_poe.api_legacy.aiohttp.ClientSession",
        return_value=session,
    ):
        api = await async_detect_api("h", "pw")
    assert isinstance(api, NetgearCheetahApi)


async def test_detect_api_modern() -> None:
    """No redirect and no /base/ login form selects the JSON CGI client."""
    session = _mock_session(None, "<html><body>login</body></html>")
    with patch(
        "custom_components.netgear_poe.api_legacy.aiohttp.ClientSession",
        return_value=session,
    ):
        api = await async_detect_api("h", "pw")
    assert isinstance(api, NetgearPoeApi)


IMAGE_LIST_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<DeviceConfiguration>
<ImageUnitList type="section">
<Entry>
<unitID>1</unitID>
<currentImage>1</currentImage>
<nextBootImage>1</nextBootImage>
<image1Version>6.0.1.16</image1Version>
<image2Version>6.0.1.30</image2Version>
<image1Description></image1Description>
<image2Description></image2Description>
</Entry>
</ImageUnitList>
</DeviceConfiguration>
</ResponseData>"""


def _load_status_xml(status: str, sent: int = 0, message: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<ResponseData>
<DeviceConfiguration>
<LoadStatus type="section">
<copyStatusType>{status}</copyStatusType>
<bytesTransfered>{sent}</bytesTransfered>
<errorMessage>{message}</errorMessage>
</LoadStatus>
</DeviceConfiguration>
</ResponseData>"""


def _image_status(image1: str, image2: str, active: str, nxt: str) -> DualImageStatus:
    return DualImageStatus(
        versions={"image1": image1, "image2": image2},
        current_active=active,
        next_active=nxt,
    )


async def test_get_image_status() -> None:
    """The ImageUnitList entry maps to slot versions and active markers."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(return_value=_parse_xml(IMAGE_LIST_XML))

    status = await api.async_get_image_status()

    assert status.versions == {"image1": "6.0.1.16", "image2": "6.0.1.30"}
    assert status.current_active == "image1"
    assert status.next_active == "image1"
    assert status.inactive == "image2"


async def test_get_image_status_without_entry_raises() -> None:
    """A response with no entry is an error, never a guessed slot."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(
        return_value=_parse_xml('<?xml version="1.0"?><ResponseData/>')
    )
    with pytest.raises(NetgearError, match="dual-image"):
        await api.async_get_image_status()


async def test_activate_image_posts_next_boot_image() -> None:
    """Activation sets nextBootImage and echoes the running image back."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(
        side_effect=[_parse_xml(IMAGE_LIST_XML), _parse_xml(SET_OK_XML)]
    )

    await api._async_activate_image("image2", "6.0.1.30")

    body = api._request.await_args.kwargs["body"]
    assert '<ImageUnitList action="set" set="set">' in body
    assert "<nextBootImage>2</nextBootImage>" in body
    assert "<currentImage>1</currentImage>" in body
    assert "<image2Description>6.0.1.30</image2Description>" in body


async def test_activate_image_failure_surfaces() -> None:
    """A non-zero statusCode aborts activation with the switch's reason."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(
        side_effect=[_parse_xml(IMAGE_LIST_XML), _parse_xml(SET_FAIL_XML)]
    )
    with pytest.raises(NetgearError, match="out of range"):
        await api._async_activate_image("image2", "6.0.1.30")


async def test_reboot_posts_reload_and_drops_session() -> None:
    """The reboot posts a Reload set and forgets the (now dead) session."""
    api = NetgearLegacyApi("host", "pw")
    api._cookie = "live"
    api._request = AsyncMock(return_value=_parse_xml(SET_OK_XML))

    await api._async_reboot()

    body = api._request.await_args.kwargs["body"]
    assert '<Reload action="set" set="set">' in body
    assert "<UnitList><UnitEntry><unitID>0</unitID></UnitEntry></UnitList>" in body
    assert api._cookie is None


async def test_reboot_swallows_the_connection_drop() -> None:
    """The switch drops the connection as it goes down; that is success."""
    api = NetgearLegacyApi("host", "pw")
    api._request = AsyncMock(side_effect=NetgearError("connection reset"))

    await api._async_reboot()  # must not raise

    assert api._cookie is None


def _yielding_sleep():
    """Patch for asyncio.sleep that still yields, so tasks can progress."""
    real_sleep = asyncio.sleep

    async def fast(_seconds: float) -> None:
        await real_sleep(0)

    return fast


async def test_upload_firmware_reports_progress_and_succeeds() -> None:
    """bytesTransfered drives the bar while the archive uploads.

    The upload runs as a task and {LoadStatus} is polled alongside it, so the
    poll count isn't fixed; the switch finishing is what ends the loop.
    """
    api = NetgearLegacyApi("host", "pw")
    finished = asyncio.Event()

    async def fake_post(image: bytes, filename: str) -> None:
        await finished.wait()

    api._async_post_archive = fake_post
    remaining = [_load_status_xml("2", sent=25), _load_status_xml("2", sent=75)]

    async def fake_request(path: str, body: str | None = None):
        if body is not None:  # the clear-LoadStatus delete
            return _parse_xml(SET_OK_XML)
        if remaining:
            return _parse_xml(remaining.pop(0))
        finished.set()  # let the upload task complete
        return _parse_xml(_load_status_xml("5", sent=100))

    api._request = AsyncMock(side_effect=fake_request)
    progress: list[int] = []

    with patch(
        "custom_components.netgear_poe.api_legacy.asyncio.sleep",
        new=_yielding_sleep(),
    ):
        await api._async_upload_firmware(b"x" * 100, "fw.ros", progress.append)

    # Progress stays inside the upload's band and never goes backwards.
    assert progress
    assert progress == sorted(progress)
    assert all(20 <= p <= 60 for p in progress)


async def test_upload_firmware_surfaces_switch_error() -> None:
    """copyStatusType 3/4 raises with the switch's own error message."""
    api = NetgearLegacyApi("host", "pw")
    api._async_post_archive = AsyncMock()

    async def fake_request(path: str, body: str | None = None):
        if body is not None:  # the clear-LoadStatus delete
            return _parse_xml(SET_OK_XML)
        return _parse_xml(_load_status_xml("3", message="Invalid image file"))

    api._request = AsyncMock(side_effect=fake_request)

    with (
        patch(
            "custom_components.netgear_poe.api_legacy.asyncio.sleep",
            new=_yielding_sleep(),
        ),
        pytest.raises(NetgearError, match="Invalid image file"),
    ):
        await api._async_upload_firmware(b"x", "fw.ros")


async def test_install_firmware_verifies_then_activates_and_reboots() -> None:
    """The staged slot is verified before anything irreversible happens."""
    api = NetgearLegacyApi("host", "pw")
    api.async_get_image_status = AsyncMock(
        side_effect=[
            _image_status("6.0.1.16", "6.0.1.16", "image1", "image1"),
            _image_status("6.0.1.16", "6.0.1.30", "image1", "image1"),
            _image_status("6.0.1.16", "6.0.1.30", "image1", "image2"),
        ]
    )
    api._async_upload_firmware = AsyncMock()
    api._async_activate_image = AsyncMock()
    api._async_reboot = AsyncMock()
    api._async_wait_for_firmware = AsyncMock()
    progress: list[int] = []

    await api.async_install_firmware(
        b"ros", "6.0.1.30", filename="fw.ros", progress=progress.append
    )

    api._async_activate_image.assert_awaited_once_with("image2", "6.0.1.30")
    api._async_reboot.assert_awaited_once()
    assert progress == sorted(progress)
    assert progress[-1] == 100


async def test_install_firmware_stops_when_slot_did_not_take_the_image() -> None:
    """If the inactive slot still holds the old version, nothing reboots."""
    api = NetgearLegacyApi("host", "pw")
    unchanged = _image_status("6.0.1.16", "6.0.1.16", "image1", "image1")
    api.async_get_image_status = AsyncMock(side_effect=[unchanged, unchanged])
    api._async_upload_firmware = AsyncMock()
    api._async_activate_image = AsyncMock()
    api._async_reboot = AsyncMock()

    with pytest.raises(NetgearError, match=r"reports 6\.0\.1\.16"):
        await api.async_install_firmware(b"ros", "6.0.1.30", filename="fw.ros")

    api._async_activate_image.assert_not_awaited()
    api._async_reboot.assert_not_awaited()


async def test_wait_for_firmware_rides_out_reboot() -> None:
    """The old version and a dead switch both keep the wait going."""
    api = NetgearLegacyApi("host", "pw")
    api.async_get_info = AsyncMock(
        side_effect=[
            SwitchInfo(name="s", model="m", firmware="6.0.1.16"),  # pre-reboot
            NetgearError("down"),
            SwitchInfo(name="s", model="m", firmware="6.0.1.30"),
        ]
    )
    progress: list[int] = []
    with patch(
        "custom_components.netgear_poe.api_legacy.asyncio.sleep", new=AsyncMock()
    ):
        await api._async_wait_for_firmware("6.0.1.30", progress.append)

    assert api.async_get_info.await_count == 3
    assert all(80 <= p <= 95 for p in progress)


async def test_wait_for_firmware_times_out_with_last_seen() -> None:
    """Booting the wrong image surfaces what the switch actually reports."""
    api = NetgearLegacyApi("host", "pw")
    api.async_get_info = AsyncMock(
        return_value=SwitchInfo(name="s", model="m", firmware="6.0.1.16")
    )
    with (
        patch(
            "custom_components.netgear_poe.api_legacy.asyncio.sleep", new=AsyncMock()
        ),
        pytest.raises(NetgearError, match=r"last reported 6\.0\.1\.16"),
    ):
        await api._async_wait_for_firmware("6.0.1.30")

    assert api.async_get_info.await_count == _REBOOT_POLL_ATTEMPTS
