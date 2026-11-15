"""Tests for the port link binary sensors."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError, PoeData
from custom_components.netgear_poe.const import DOMAIN

from .conftest import MOCK_HOST, MOCK_PASSWORD, make_poe_data, setup_integration

PORT_1_LINK = "binary_sensor.boiler_switch_port_1_driveway_cam_link"
PORT_2_LINK = "binary_sensor.boiler_switch_port_2_link"
STALLED = "binary_sensor.boiler_switch_poe_controller_stalled"


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
    mock_link_monitor.async_get_port_info.return_value = ({}, {})
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(PORT_1_LINK)
    assert state is not None
    assert state.state == "unavailable"


async def test_snmp_names_override_port_alias(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Port names from SNMP ifAlias are applied to the entities."""
    mock_link_monitor.async_get_port_info.return_value = (
        {1: True, 2: True},
        {2: "garage-cam"},
    )
    await setup_integration(hass, mock_config_entry)

    # Port 2 had no alias in the base data; SNMP supplies its name.
    state = hass.states.get("switch.boiler_switch_port_2_garage_cam_poe")
    assert state is not None


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
    # The stall problem sensor is not SNMP-gated; it exists regardless.
    assert hass.states.get(STALLED) is not None


async def test_poe_controller_stalled_sensor(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The problem sensor turns on once the PoE telemetry stall trips."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(STALLED)
    assert state is not None
    assert state.state == "off"

    # PoE read hangs while the switch stays reachable (async_get_info still OK).
    mock_api.async_get_data.side_effect = NetgearError("timeout")
    coordinator = mock_config_entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await coordinator.async_refresh()  # second miss trips the threshold
    await hass.async_block_till_done()

    state = hass.states.get(STALLED)
    assert state.state == "on"

    # Recovery clears it.
    mock_api.async_get_data.side_effect = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(STALLED).state == "off"


async def test_non_poe_ports_get_disabled_link_sensors(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Every port with link data gets a sensor; only PoE ports start enabled."""
    mock_link_monitor.async_get_port_info.return_value = (
        {1: True, 2: False, 11: True, 12: False},
        {1: "driveway cam", 11: "Shure 1&2"},
    )
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)

    # PoE ports keep their existing unique_ids and stay enabled.
    for port in (1, 2):
        entity_id = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_port_{port}_link"
        )
        assert entity_id is not None
        assert registry.async_get(entity_id).disabled_by is None
    assert hass.states.get(PORT_1_LINK).state == "on"

    # Non-PoE ports exist in the registry, disabled by the integration and
    # named from their SNMP ifAlias like the PoE ports' sensors.
    for port in (11, 12):
        entity_id = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_port_{port}_link"
        )
        assert entity_id is not None
        entry = registry.async_get(entity_id)
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_port_11_link"
        )
        == "binary_sensor.boiler_switch_port_11_shure_1_2_link"
    )


async def test_enabled_non_poe_link_sensor_follows_traps(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Once enabled, a non-PoE port's sensor is named and updated by traps."""
    mock_link_monitor.async_get_port_info.return_value = (
        {1: True, 2: False, 12: True},
        {12: "Shure 3&4"},
    )
    mock_config_entry.add_to_hass(hass)
    # The owner enabled it earlier: a registry entry that isn't disabled.
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{mock_config_entry.entry_id}_port_12_link",
        suggested_object_id="boiler_switch_port_12_shure_3_4_link",
        config_entry=mock_config_entry,
    )
    await setup_integration(hass, mock_config_entry)
    entity_id = "binary_sensor.boiler_switch_port_12_shure_3_4_link"

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["friendly_name"] == "boiler-switch Port 12 (Shure 3&4) link"

    # The receiver is unplugged: the switch's next poll will agree, but the
    # trap must move the entity on its own, before any refresh runs.
    mock_link_monitor.async_get_port_info.return_value = (
        {1: True, 2: False, 12: False},
        {12: "Shure 3&4"},
    )
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.apply_link_trap(12, False)
    assert hass.states.get(entity_id).state == "off"
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"


async def test_link_sensors_added_when_snmp_answers_late(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Ports SNMP didn't report at setup get their sensors once it does."""
    mock_link_monitor.async_get_port_info.return_value = ({}, {})
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)
    port_9 = f"{mock_config_entry.entry_id}_port_9_link"
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, port_9) is None

    mock_link_monitor.async_get_port_info.return_value = (
        {1: True, 2: False, 9: True},
        {9: "x32-sanctuary"},
    )
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, port_9)
    assert entity_id is not None
    assert registry.async_get(entity_id).disabled_by is not None
    assert hass.states.get(PORT_1_LINK).state == "on"


async def test_non_poe_ports_wait_for_poe_data(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Without PoE data, ports can't be told apart, so none are added yet."""
    mock_api.async_get_data.return_value = PoeData()
    mock_link_monitor.async_get_port_info.return_value = ({1: True, 9: True}, {})
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)
    for port in (1, 9):
        assert (
            registry.async_get_entity_id(
                "binary_sensor",
                DOMAIN,
                f"{mock_config_entry.entry_id}_port_{port}_link",
            )
            is None
        )

    # PoE data arrives: port 1 is PoE (enabled), port 9 is not (disabled).
    mock_api.async_get_data.return_value = make_poe_data()
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    port_1 = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_port_1_link"
    )
    port_9 = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_port_9_link"
    )
    assert registry.async_get(port_1).disabled_by is None
    assert registry.async_get(port_9).disabled_by is not None
