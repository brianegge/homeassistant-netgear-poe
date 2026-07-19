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


async def test_poe_stall_keeps_switch_available_and_flags_after_threshold(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A hung PoE read on a reachable switch flags a stall, not an outage.

    While management reads (async_get_info) still answer, the device stays
    available with its last-known PoE data and the problem flag turns on only
    after POE_STALL_THRESHOLD consecutive failures — then clears on recovery.
    """
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.poe_stalled is False

    # The PoE telemetry query now hangs, but the switch still answers info.
    mock_api.async_get_data.side_effect = NetgearError("timeout")

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True  # still available
    assert coordinator.poe_stalled is False  # one miss is below the threshold

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.poe_stalled is True  # second miss trips it
    # Last-known PoE data is retained (power keeps flowing while telemetry hangs).
    assert coordinator.data.ports[1].power_watts == 6.5

    # Recovery clears the flag.
    mock_api.async_get_data.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.poe_stalled is False


async def test_poe_read_failure_when_unreachable_marks_unavailable(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """When neither PoE nor management reads answer, the switch is down.

    That is a real outage — the coordinator fails (entities unavailable) and
    must not masquerade as a PoE-controller stall.
    """
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator

    mock_api.async_get_data.side_effect = NetgearError("timeout")
    mock_api.async_get_info.side_effect = NetgearError("timeout")

    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert coordinator.poe_stalled is False


async def test_setup_succeeds_when_poe_wedged_at_startup(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A switch wedged before HA starts still loads, so it can be rebooted.

    The PoE read hangs from the very first refresh, but the switch answers
    management reads. Rather than failing setup (which would hide the Reboot
    button behind a never-ready entry), the entry loads with empty PoE data:
    the device-level Reboot button and stall sensor come up so the switch can
    be recovered, while per-port entities (nothing to read) are absent.
    """
    mock_api.async_get_data.side_effect = NetgearError("timeout")

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    # The recovery controls are present...
    assert hass.states.get("button.boiler_switch_reboot") is not None
    assert (
        hass.states.get("binary_sensor.boiler_switch_poe_controller_stalled")
        is not None
    )
    # ...while per-port entities, which need PoE data, are not created yet.
    assert hass.states.get("switch.boiler_switch_port_1_driveway_cam_poe") is None
