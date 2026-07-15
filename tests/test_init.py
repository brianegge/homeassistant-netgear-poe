"""Tests for Netgear PoE integration setup."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError
from custom_components.netgear_poe.const import DOMAIN
from custom_components.netgear_poe.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import MOCK_FIRMWARE, MOCK_MODEL, MOCK_SYS_NAME, setup_integration


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setting up and unloading the entry."""
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    # sensor from consumption_watts
    state = hass.states.get("sensor.boiler_switch_poe_power")
    assert state is not None
    assert state.state == "42.0"

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_api.async_close.assert_awaited()


async def test_device_registry_entry(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the device carries the switch's identity, including firmware."""
    await setup_integration(hass, mock_config_entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, mock_config_entry.entry_id)}
    )
    assert device is not None
    assert device.name == MOCK_SYS_NAME
    assert device.model == MOCK_MODEL
    assert device.sw_version == MOCK_FIRMWARE
    assert device.manufacturer == "Netgear"


async def test_diagnostics(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test diagnostics include switch identity and redact credentials."""
    await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diag["sys_name"] == MOCK_SYS_NAME
    assert diag["model"] == MOCK_MODEL
    assert diag["firmware"] == MOCK_FIRMWARE
    assert diag["entry_data"]["password"] == "**REDACTED**"
    assert diag["entry_data"]["community"] == "**REDACTED**"
    assert diag["data"]["ports"][1]["alias"] == "driveway cam"


async def test_setup_retries_when_unreachable(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setup goes to retry when SNMP is down."""
    mock_api.async_get_info.side_effect = NetgearError("timeout")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
