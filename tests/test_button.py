"""Tests for the Netgear PoE power-cycle button."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError

from .conftest import setup_integration

BUTTON_ENTITY = "button.boiler_switch_port_1_driveway_cam_power_cycle"


async def test_power_cycle(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the button triggers the native PoE reset."""
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": BUTTON_ENTITY},
        blocking=True,
    )
    mock_api.async_power_cycle_port.assert_awaited_once_with(1)


async def test_power_cycle_failure(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test failure during power cycle raises."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_power_cycle_port.side_effect = NetgearError("timeout")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": BUTTON_ENTITY},
            blocking=True,
        )
