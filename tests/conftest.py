"""Shared fixtures for Netgear PoE Switch tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import loader
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import PoeData, PoePort, SwitchInfo
from custom_components.netgear_poe.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass: HomeAssistant) -> None:
    """Enable custom integrations in all tests."""
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)


@pytest.fixture(autouse=True)
def no_discovery() -> Generator[None]:
    """Don't start the NSDP discovery scanner in most tests."""
    with patch("custom_components.netgear_poe._async_start_discovery"):
        yield


MOCK_HOST = "boiler-switch.home"
MOCK_PASSWORD = "mock-password"  # NOSONAR
MOCK_COMMUNITY = "mock-community"
MOCK_CONFIG = {
    "host": MOCK_HOST,
    "password": MOCK_PASSWORD,
    "community": MOCK_COMMUNITY,
    "enable_traps": True,
}
MOCK_SYS_NAME = "boiler-switch"
MOCK_MODEL = "GS728TPv2"
MOCK_FIRMWARE = "6.0.8.15"


def make_poe_data(
    *,
    port_1_enabled: bool = True,
    port_2_enabled: bool = False,
    consumption_watts: float | None = 42.0,
) -> PoeData:
    """Create mock PoE data with two ports."""
    return PoeData(
        ports={
            1: PoePort(
                port=1,
                admin_enabled=port_1_enabled,
                detection_status="delivering" if port_1_enabled else "disabled",
                power_watts=6.5 if port_1_enabled else 0.0,
                alias="driveway cam",
            ),
            2: PoePort(
                port=2,
                admin_enabled=port_2_enabled,
                detection_status="delivering" if port_2_enabled else "disabled",
                power_watts=0.0,
                alias="",
            ),
        },
        consumption_watts=consumption_watts,
    )


@pytest.fixture
def mock_link_monitor() -> Generator[MagicMock]:
    """Mock the SNMP link monitor."""
    with patch(
        "custom_components.netgear_poe.SnmpLinkMonitor", autospec=True
    ) as monitor_class:
        monitor = monitor_class.return_value
        monitor.async_get_port_info = AsyncMock(
            return_value=({1: True, 2: False}, {1: "driveway cam"})
        )
        monitor.async_close = AsyncMock()
        yield monitor


@pytest.fixture
def mock_trap_receiver() -> Generator[MagicMock]:
    """Mock the SNMP trap receiver and source-IP lookup."""
    with (
        patch(
            "custom_components.netgear_poe.SnmpTrapReceiver", autospec=True
        ) as rx_class,
        patch(
            "custom_components.netgear_poe._get_source_ip",
            return_value="192.168.254.30",
        ),
    ):
        rx = rx_class.return_value
        rx.async_start = AsyncMock()
        rx.async_stop = AsyncMock()
        # Expose the on_link_change callback passed at construction
        rx_class.side_effect = lambda **kw: _store_callback(rx, kw)
        yield rx


def _store_callback(rx: MagicMock, kwargs: dict) -> MagicMock:
    """Record the on_link_change callback so tests can invoke it."""
    rx.on_link_change = kwargs.get("on_link_change")
    return rx


@pytest.fixture
def mock_api(
    mock_link_monitor: MagicMock, mock_trap_receiver: MagicMock
) -> Generator[MagicMock]:
    """Mock the switch api for both __init__ and config_flow.

    Both call sites go through async_detect_api, which is patched to hand
    back the same mocked client.
    """
    api = MagicMock()
    api.host = MOCK_HOST
    # MagicMock would answer truthy for any attribute; the default fixture
    # models the JSON CGI backend, which cannot flash firmware.
    api.supports_firmware_install = False
    api.async_get_info = AsyncMock(
        return_value=SwitchInfo(
            name=MOCK_SYS_NAME, model=MOCK_MODEL, firmware=MOCK_FIRMWARE
        )
    )
    api.async_login = AsyncMock()
    api.async_get_data = AsyncMock(return_value=make_poe_data())
    api.async_set_port_enabled = AsyncMock()
    api.async_set_port_name = AsyncMock()
    api.async_power_cycle_port = AsyncMock()
    api.async_reboot = AsyncMock()
    api.async_ensure_trap_destination = AsyncMock()
    api.async_close = AsyncMock()
    with (
        patch(
            "custom_components.netgear_poe.async_detect_api",
            new=AsyncMock(return_value=api),
        ),
        patch(
            "custom_components.netgear_poe.config_flow.async_detect_api",
            new=AsyncMock(return_value=api),
        ),
    ):
        yield api


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_SYS_NAME,
        data=MOCK_CONFIG,
        unique_id=MOCK_HOST,
    )


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up the integration with a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def parse_upload_payload(payload: object) -> list[tuple[str, object]]:
    """Parse a _ProgressUpload multipart body into ordered (name, value) pairs.

    Text values come back as str, the file part as bytes. Lets the upload
    tests assert on the real streamed body now that it is built by hand
    (a plain bytes payload) rather than an aiohttp FormData.
    """
    content_type = payload.content_type  # type: ignore[attr-defined]
    boundary = content_type.split("boundary=")[1].encode()
    body = payload._value  # type: ignore[attr-defined]
    fields: list[tuple[str, object]] = []
    for raw in body.split(b"--" + boundary):
        if b"Content-Disposition" not in raw:
            continue
        head, _, value = raw.lstrip(b"\r\n").partition(b"\r\n\r\n")
        if value.endswith(b"\r\n"):
            value = value[:-2]
        head_text = head.decode("latin1")
        name = head_text.split('name="')[1].split('"')[0]
        is_file = "filename=" in head_text
        fields.append((name, value if is_file else value.decode("latin1")))
    return fields
