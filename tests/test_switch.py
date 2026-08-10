"""Tests for the Netgear PoE switch platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError, VlanMembership
from custom_components.netgear_poe.const import (
    DOMAIN,
    SERVICE_GET_VLAN_MEMBERSHIP,
    SERVICE_SET_PORT_NAME,
    SERVICE_SET_VLAN_MEMBERSHIP,
)

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
    """Test turning PoE off writes through to the switch's web API."""
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
    """Test turning PoE on writes through to the switch's web API."""
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
    """Test a failed write to the switch raises a HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_set_port_enabled.side_effect = NetgearError("noAccess")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": PORT_1_ENTITY},
            blocking=True,
        )


async def test_set_vlan_membership(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The set_vlan_membership action maps the state name to the wire value."""
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VLAN_MEMBERSHIP,
        {"entity_id": PORT_1_ENTITY, "vlan": 21, "membership": "tagged"},
        blocking=True,
    )
    mock_api.async_set_vlan_port_membership.assert_awaited_with(21, 1, 2)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VLAN_MEMBERSHIP,
        {"entity_id": PORT_2_ENTITY, "vlan": 21, "membership": "none"},
        blocking=True,
    )
    mock_api.async_set_vlan_port_membership.assert_awaited_with(21, 2, 0)


async def test_get_vlan_membership_response(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The get_vlan_membership action answers the whole VLAN's map by name."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_get_vlan_membership.return_value = VlanMembership(
        vid=21,
        name="cctv",
        ports={1: 2, 2: 0, 3: 3},
        lags={1: 1},
        vlans=[1, 21],
    )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_VLAN_MEMBERSHIP,
        {"entity_id": PORT_1_ENTITY, "vlan": 21},
        blocking=True,
        return_response=True,
    )

    mock_api.async_get_vlan_membership.assert_awaited_with(21)
    assert response == {
        PORT_1_ENTITY: {
            "vlan": 21,
            "name": "cctv",
            "port_membership": "tagged",
            "ports": {1: "tagged", 2: "none", 3: "dynamic"},
            "lags": {1: "untagged"},
            "configured_vlans": [1, 21],
        }
    }


async def test_vlan_membership_failure(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A switch error surfaces as a HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)
    mock_api.async_set_vlan_port_membership.side_effect = NetgearError("denied")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_VLAN_MEMBERSHIP,
            {"entity_id": PORT_1_ENTITY, "vlan": 21, "membership": "tagged"},
            blocking=True,
        )


async def test_vlan_membership_unsupported_backend(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Backends without vlan_membership answer with a clear error."""
    mock_api.supports_vlan_membership = False
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_VLAN_MEMBERSHIP,
            {"entity_id": PORT_1_ENTITY, "vlan": 21, "membership": "none"},
            blocking=True,
        )
    mock_api.async_set_vlan_port_membership.assert_not_awaited()


async def test_web_port_names_survive_a_dead_snmp_agent(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A wedged SNMP agent must not cost the ports their web-CGI names.

    The agent on this firmware hangs and answers nothing; ifAlias is then
    empty, so the names the API already read over the web UI have to stand.
    """
    mock_link_monitor.async_get_port_info.return_value = ({}, {})
    await setup_integration(hass, mock_config_entry)

    # The web CGI fetch stays enabled, so the alias is still there to fall
    # back on rather than being suppressed in favor of a source that is down.
    assert mock_api.web_port_names_enabled is True
    state = hass.states.get(PORT_1_ENTITY)
    assert state is not None
    assert (
        state.attributes["friendly_name"] == "boiler-switch Port 1 (driveway cam) PoE"
    )


async def test_port_name_follows_a_rename_without_a_reload(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Names are rebuilt per state write, so a later alias is picked up."""
    mock_link_monitor.async_get_port_info.return_value = ({}, {})
    await setup_integration(hass, mock_config_entry)

    # SNMP comes back (or the port is renamed) and reports a new ifAlias.
    mock_link_monitor.async_get_port_info.return_value = ({1: True}, {1: "boiler pump"})
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(PORT_1_ENTITY)
    assert state is not None
    assert state.attributes["friendly_name"] == "boiler-switch Port 1 (boiler pump) PoE"
    # The entity_id was generated at first add and must not move under the user.
    assert hass.states.get("switch.boiler_switch_port_1_boiler_pump_poe") is None


async def test_web_name_fetch_yields_to_snmp_and_returns_when_it_dies(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The CGI name page is only read while SNMP is not supplying names."""
    await setup_integration(hass, mock_config_entry)

    # SNMP answered with ifAlias names, so the duplicate CGI read is off.
    assert mock_api.web_port_names_enabled is False

    # Agent wedges: the CGI becomes the name source again.
    mock_link_monitor.async_get_port_info.return_value = ({}, {})
    coordinator = mock_config_entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_api.web_port_names_enabled is True

    # And it yields again once SNMP recovers.
    mock_link_monitor.async_get_port_info.return_value = (
        {1: True},
        {1: "driveway cam"},
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_api.web_port_names_enabled is False
