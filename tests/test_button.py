"""Tests for the Netgear PoE power-cycle button."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import SnmpError

from .conftest import setup_integration

BUTTON_ENTITY = "button.boiler_switch_port_1_driveway_cam_power_cycle"


async def test_power_cycle(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the button turns the port off then back on."""
    await setup_integration(hass, mock_config_entry)

    with patch("custom_components.netgear_poe.button.asyncio.sleep") as mock_sleep:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": BUTTON_ENTITY},
            blocking=True,
        )

    assert mock_api.async_set_port_enabled.await_args_list == [
        call(1, False),
        call(1, True),
    ]
    mock_sleep.assert_awaited_once()


async def test_power_cycle_failure(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test SNMP failure during power cycle raises."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_set_port_enabled.side_effect = SnmpError("timeout")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": BUTTON_ENTITY},
            blocking=True,
        )
