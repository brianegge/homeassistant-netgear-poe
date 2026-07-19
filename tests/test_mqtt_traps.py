"""Tests for the snmptrap2mqtt MQTT trap mode."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.netgear_poe.const import DOMAIN

from .conftest import (
    MOCK_COMMUNITY,
    MOCK_CONFIG,
    MOCK_CONFIG_LEGACY,
    MOCK_SYS_NAME,
    setup_integration,
)

PORT_1_LINK = "binary_sensor.boiler_switch_port_1_driveway_cam_link"
SWITCH_IP = "192.168.254.250"
BRIDGE_HOST = "192.168.254.40"
TRAP_TOPIC = "snmptrap2mqtt/boiler-switch/trap"

MOCK_CONFIG_MQTT = {
    **MOCK_CONFIG,
    "trap_mode": "mqtt",
    "trap_bridge_host": BRIDGE_HOST,
}


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The mqtt_mock fixture leaves HA's own MQTT misc-periodic timer behind."""
    return True


@pytest.fixture
def mock_config_entry_mqtt() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_SYS_NAME,
        data=MOCK_CONFIG_MQTT,
        unique_id=MOCK_CONFIG_MQTT["host"],
    )


@pytest.fixture(autouse=True)
def resolve_entry_host() -> None:
    """The entry uses a hostname; the bridge reports the switch's IP."""
    with patch(
        "custom_components.netgear_poe.mqtt_traps._resolve_ip",
        return_value=SWITCH_IP,
    ):
        yield


def _trap_payload(event_type: str, if_index: int, source_ip: str = SWITCH_IP) -> str:
    return json.dumps(
        {
            "event_type": event_type,
            "trap_oid": "1.3.6.1.6.3.1.1.5.3",
            "source_ip": source_ip,
            "target": "boiler-switch",
            "if_index": if_index,
            "uptime": 123456,
            "varbinds": {},
        }
    )


async def test_mqtt_trap_updates_entity(
    hass: HomeAssistant,
    mqtt_mock,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
    mock_config_entry_mqtt: MockConfigEntry,
) -> None:
    """A bridge linkDown trap flips the link sensor; local receiver stays off."""
    await setup_integration(hass, mock_config_entry_mqtt)

    # MQTT mode must not bind UDP 162, and must register the *bridge* host.
    mock_trap_receiver.async_start.assert_not_awaited()
    mock_api.async_ensure_trap_destination.assert_awaited_once_with(
        BRIDGE_HOST, MOCK_COMMUNITY
    )

    assert hass.states.get(PORT_1_LINK).state == "on"
    async_fire_mqtt_message(hass, TRAP_TOPIC, _trap_payload("linkDown", 1))
    await hass.async_block_till_done()
    assert hass.states.get(PORT_1_LINK).state == "off"

    async_fire_mqtt_message(hass, TRAP_TOPIC, _trap_payload("linkUp", 1))
    await hass.async_block_till_done()
    assert hass.states.get(PORT_1_LINK).state == "on"


async def test_mqtt_trap_source_mismatch_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
    mock_config_entry_mqtt: MockConfigEntry,
) -> None:
    """Traps from another switch (different source IP) are ignored."""
    await setup_integration(hass, mock_config_entry_mqtt)

    assert hass.states.get(PORT_1_LINK).state == "on"
    async_fire_mqtt_message(
        hass,
        "snmptrap2mqtt/attic-switch/trap",
        json.dumps(
            {
                "event_type": "linkDown",
                "source_ip": "192.168.254.251",
                "target": "attic-switch",
                "if_index": 1,
            }
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(PORT_1_LINK).state == "on"


async def test_mqtt_trap_lag_and_junk_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
    mock_config_entry_mqtt: MockConfigEntry,
) -> None:
    """Non-physical ifIndex and malformed payloads do nothing."""
    await setup_integration(hass, mock_config_entry_mqtt)

    async_fire_mqtt_message(hass, TRAP_TOPIC, _trap_payload("linkDown", 1000))
    async_fire_mqtt_message(hass, TRAP_TOPIC, "not-json")
    await hass.async_block_till_done()
    assert hass.states.get(PORT_1_LINK).state == "on"


async def test_mqtt_poe_change_triggers_refresh(
    hass: HomeAssistant,
    mqtt_mock,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
    mock_config_entry_mqtt: MockConfigEntry,
) -> None:
    """poePortChange asks the coordinator for an authoritative refresh."""
    await setup_integration(hass, mock_config_entry_mqtt)

    calls_before = mock_api.async_get_data.await_count
    async_fire_mqtt_message(hass, TRAP_TOPIC, _trap_payload("poePortChange", 1))
    await hass.async_block_till_done()
    assert mock_api.async_get_data.await_count > calls_before


async def test_mqtt_mode_without_bridge_host_skips_registration(
    hass: HomeAssistant,
    mqtt_mock,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
) -> None:
    """No bridge host configured -> no trap destination is written."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_SYS_NAME,
        data={**MOCK_CONFIG_MQTT, "trap_bridge_host": ""},
        unique_id=MOCK_CONFIG_MQTT["host"],
    )
    await setup_integration(hass, entry)
    mock_api.async_ensure_trap_destination.assert_not_awaited()


async def test_legacy_enable_traps_false_disables_all(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
) -> None:
    """Pre-trap_mode entries with enable_traps: false stay fully disabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_SYS_NAME,
        data={**MOCK_CONFIG_LEGACY, "enable_traps": False},
        unique_id=MOCK_CONFIG_LEGACY["host"],
    )
    await setup_integration(hass, entry)
    mock_trap_receiver.async_start.assert_not_awaited()
    mock_api.async_ensure_trap_destination.assert_not_awaited()


async def test_legacy_enable_traps_true_stays_local(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_trap_receiver: MagicMock,
) -> None:
    """Pre-trap_mode entries with enable_traps: true keep the local receiver."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_SYS_NAME,
        data=MOCK_CONFIG_LEGACY,
        unique_id=MOCK_CONFIG_LEGACY["host"],
    )
    await setup_integration(hass, entry)
    mock_trap_receiver.async_start.assert_awaited_once()
