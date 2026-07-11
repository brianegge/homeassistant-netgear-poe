"""Shared fixtures for Netgear PoE Switch tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import loader
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import PoeData, PoePort
from custom_components.netgear_poe.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass: HomeAssistant) -> None:
    """Enable custom integrations in all tests."""
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)


MOCK_HOST = "boiler-switch.home"
MOCK_COMMUNITY = "mock-community"  # NOSONAR
MOCK_CONFIG = {
    "host": MOCK_HOST,
    "community": MOCK_COMMUNITY,
    "write_community": "",
}
MOCK_SYS_NAME = "boiler-switch"
MOCK_SYS_DESCR = (
    "NETGEAR 24-Port Gigabit PoE+ Smart Managed Pro Switch with 4 SFP Ports "
    "(GS728TPv2), Software Version 6.0.0.45"
)


def make_poe_data(
    *,
    port_1_enabled: bool = True,
    port_2_enabled: bool = False,
    consumption_watts: int | None = 42,
) -> PoeData:
    """Create mock PoE data with two ports."""
    return PoeData(
        ports={
            1: PoePort(
                port=1,
                admin_enabled=port_1_enabled,
                detection_status="delivering_power" if port_1_enabled else "disabled",
                alias="driveway cam",
            ),
            2: PoePort(
                port=2,
                admin_enabled=port_2_enabled,
                detection_status="delivering_power" if port_2_enabled else "disabled",
                alias="",
            ),
        },
        consumption_watts=consumption_watts,
        sys_name=MOCK_SYS_NAME,
        sys_descr=MOCK_SYS_DESCR,
    )


@pytest.fixture
def mock_api() -> Generator[MagicMock]:
    """Mock the SNMP api for both __init__ and config_flow."""
    with (
        patch(
            "custom_components.netgear_poe.NetgearPoeApi", autospec=True
        ) as api_class,
        patch(
            "custom_components.netgear_poe.config_flow.NetgearPoeApi",
            new=api_class,
        ),
    ):
        api = api_class.return_value
        api.host = MOCK_HOST
        api.async_get_info = AsyncMock(return_value=(MOCK_SYS_NAME, MOCK_SYS_DESCR))
        api.async_get_data = AsyncMock(return_value=make_poe_data())
        api.async_set_port_enabled = AsyncMock()
        api.async_close = AsyncMock()
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


async def setup_integration(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Set up the integration with a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
