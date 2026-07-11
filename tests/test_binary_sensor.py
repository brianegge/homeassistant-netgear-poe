"""Tests for the port link binary sensors."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.const import DOMAIN

from .conftest import MOCK_HOST, MOCK_PASSWORD, setup_integration

PORT_1_LINK = "binary_sensor.boiler_switch_port_1_driveway_cam_link"
PORT_2_LINK = "binary_sensor.boiler_switch_port_2_link"


async def test_link_sensors(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test link sensors reflect SNMP ifOperStatus."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(PORT_1_LINK)
    assert state is not None
    assert state.state == "on"

    state = hass.states.get(PORT_2_LINK)
    assert state is not None
    assert state.state == "off"


async def test_link_sensors_unavailable_when_snmp_down(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test link sensors go unavailable when SNMP returns nothing."""
    mock_link_monitor.async_get_link_states.return_value = {}
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(PORT_1_LINK)
    assert state is not None
    assert state.state == "unavailable"


async def test_no_link_sensors_without_community(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
) -> None:
    """Test no link sensors are created when SNMP is not configured."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="boiler-switch",
        data={"host": MOCK_HOST, "password": MOCK_PASSWORD, "community": ""},
        unique_id=MOCK_HOST,
    )
    await setup_integration(hass, entry)
    assert hass.states.get(PORT_1_LINK) is None
