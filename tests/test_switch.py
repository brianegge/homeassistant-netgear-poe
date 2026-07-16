"""Tests for the Netgear PoE switch platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError
from custom_components.netgear_poe.const import DOMAIN, SERVICE_SET_PORT_NAME

from .conftest import setup_integration

PORT_1_ENTITY = "switch.boiler_switch_port_1_driveway_cam_poe"
PORT_2_ENTITY = "switch.boiler_switch_port_2_poe"


async def test_switch_states(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test switch entities reflect PoE admin state."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(PORT_1_ENTITY)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["detection_status"] == "delivering"
    assert state.attributes["port"] == 1
    assert state.attributes["power_watts"] == 6.5

    state = hass.states.get(PORT_2_ENTITY)
    assert state is not None
    assert state.state == "off"


async def test_switch_turn_off(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test turning PoE off sends the SNMP set."""
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": PORT_1_ENTITY},
        blocking=True,
    )
    mock_api.async_set_port_enabled.assert_awaited_with(1, False)


async def test_switch_turn_on(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test turning PoE on sends the SNMP set."""
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": PORT_2_ENTITY},
        blocking=True,
    )
    mock_api.async_set_port_enabled.assert_awaited_with(2, True)


async def test_set_port_name(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the set_port_name action writes the description to the switch."""
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_PORT_NAME,
        {"entity_id": PORT_1_ENTITY, "name": "garage-cam"},
        blocking=True,
    )
    mock_api.async_set_port_name.assert_awaited_with(1, "garage-cam")


async def test_set_port_name_failure(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a switch error surfaces as a HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_set_port_name.side_effect = NetgearError("denied")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_PORT_NAME,
            {"entity_id": PORT_1_ENTITY, "name": "garage-cam"},
            blocking=True,
        )


async def test_switch_set_failure(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test SNMP set failure raises a HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_set_port_enabled.side_effect = NetgearError("noAccess")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": PORT_1_ENTITY},
            blocking=True,
        )
