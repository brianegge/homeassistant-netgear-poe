"""Tests for the SNMP trap receiver decode logic and coordinator wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.trap_receiver import (
    OID_IF_INDEX,
    OID_LINK_DOWN,
    OID_LINK_UP,
    OID_SNMP_TRAP,
    SnmpTrapReceiver,
    _extract_if_index,
)

from .conftest import setup_integration

PORT_1_LINK = "binary_sensor.boiler_switch_port_1_driveway_cam_link"


def _make_binds(trap_oid: str, if_index: int | None) -> list:
    binds = [(OID_SNMP_TRAP, trap_oid)]
    if if_index is not None:
        binds.append((f"{OID_IF_INDEX}.{if_index}", if_index))
    return binds


def test_extract_if_index() -> None:
    """ifIndex is pulled from an indexed OID."""
    assert _extract_if_index({f"{OID_IF_INDEX}.7": 7}) == 7
    assert _extract_if_index({OID_IF_INDEX: 3}) == 3
    assert _extract_if_index({"1.2.3": 9}) is None


def test_receiver_decodes_link_down() -> None:
    """A linkDown trap fires on_link_change(port, False)."""
    events: list[tuple[int, bool]] = []
    rx = SnmpTrapReceiver(
        community="test-community",
        on_link_change=lambda port, up: events.append((port, up)),
    )
    rx._on_trap(None, None, None, None, _make_binds(OID_LINK_DOWN, 5), None)
    rx._on_trap(None, None, None, None, _make_binds(OID_LINK_UP, 5), None)
    assert events == [(5, False), (5, True)]


def test_receiver_ignores_non_physical_and_unknown() -> None:
    """LAG ports and non-link traps are ignored."""
    events: list[tuple[int, bool]] = []
    rx = SnmpTrapReceiver(
        community="test-community",
        on_link_change=lambda port, up: events.append((port, up)),
    )
    rx._on_trap(None, None, None, None, _make_binds(OID_LINK_DOWN, 1000), None)
    rx._on_trap(None, None, None, None, _make_binds("1.2.3.4", 5), None)
    assert events == []


async def test_trap_updates_entity(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A trap delivered to the coordinator updates the link sensor."""
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get(PORT_1_LINK).state == "on"

    # The integration registered its callback with the (mocked) receiver.
    on_link_change = mock_trap_receiver.on_link_change
    assert on_link_change is not None

    on_link_change(1, False)
    await hass.async_block_till_done()
    assert hass.states.get(PORT_1_LINK).state == "off"


async def test_trap_destination_registered(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setup registers this host as a trap destination and starts the listener."""
    await setup_integration(hass, mock_config_entry)

    mock_trap_receiver.async_start.assert_awaited_once()
    mock_api.async_ensure_trap_destination.assert_awaited_once_with(
        "192.168.254.30", "mock-community"
    )
