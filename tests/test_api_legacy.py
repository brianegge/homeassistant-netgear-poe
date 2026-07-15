"""Tests for the legacy xui client in api_legacy.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.netgear_poe.api import NetgearError, NetgearPoeApi
from custom_components.netgear_poe.api_base_ui import NetgearBaseUiApi
from custom_components.netgear_poe.api_legacy import (
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
    session = MagicMock()
    session.get = AsyncMock(return_value=resp)
    session.close = AsyncMock()
    return session


async def test_detect_api_legacy() -> None:
    """A /csbe<id>/ redirect selects the legacy client with its prefix."""
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


async def test_detect_api_base_ui() -> None:
    """A /base/ login form selects the classic web-UI client (GS110TP)."""
    session = _mock_session(None, '<FORM METHOD="POST" ACTION="/base/main_login.html">')
    with patch(
        "custom_components.netgear_poe.api_legacy.aiohttp.ClientSession",
        return_value=session,
    ):
        api = await async_detect_api("h", "pw")
    assert isinstance(api, NetgearBaseUiApi)


async def test_detect_api_modern() -> None:
    """No redirect and no /base/ login form selects the JSON CGI client."""
    session = _mock_session(None, "<html><body>login</body></html>")
    with patch(
        "custom_components.netgear_poe.api_legacy.aiohttp.ClientSession",
        return_value=session,
    ):
        api = await async_detect_api("h", "pw")
    assert isinstance(api, NetgearPoeApi)
