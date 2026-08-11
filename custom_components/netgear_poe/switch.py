"""Switch platform for Netgear PoE ports."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .api import VLAN_NOT_MEMBER, VLAN_TAGGED, VLAN_UNTAGGED, NetgearError
from .const import (
    DOMAIN,
    PORT_DUPLEXES,
    PORT_SPEEDS,
    SERVICE_GET_VLAN_MEMBERSHIP,
    SERVICE_SET_PORT_NAME,
    SERVICE_SET_PORT_SPEED,
    SERVICE_SET_VLAN_MEMBERSHIP,
    VLAN_MEMBERSHIP_DYNAMIC,
    VLAN_MEMBERSHIP_NONE,
    VLAN_MEMBERSHIP_STATES,
    VLAN_MEMBERSHIP_TAGGED,
    VLAN_MEMBERSHIP_UNTAGGED,
)
from .entity import NetgearPoePortEntity

ATTR_VLAN = "vlan"
ATTR_MEMBERSHIP = "membership"
ATTR_SPEED = "speed"
ATTR_DUPLEX = "duplex"
ATTR_AUTONEG = "autoneg"
ATTR_PORT = "port"

_MEMBERSHIP_TO_STATE = {
    VLAN_MEMBERSHIP_NONE: VLAN_NOT_MEMBER,
    VLAN_MEMBERSHIP_UNTAGGED: VLAN_UNTAGGED,
    VLAN_MEMBERSHIP_TAGGED: VLAN_TAGGED,
}
_STATE_TO_MEMBERSHIP = {
    VLAN_NOT_MEMBER: VLAN_MEMBERSHIP_NONE,
    VLAN_UNTAGGED: VLAN_MEMBERSHIP_UNTAGGED,
    VLAN_TAGGED: VLAN_MEMBERSHIP_TAGGED,
    3: VLAN_MEMBERSHIP_DYNAMIC,
}
_VLAN_ID = vol.All(vol.Coerce(int), vol.Range(min=1, max=4094))


def _membership_names(states: dict[int, int]) -> dict[int, str]:
    """Map raw membership states to their HA-facing names."""
    return {
        port: _STATE_TO_MEMBERSHIP.get(state, VLAN_MEMBERSHIP_NONE)
        for port, state in states.items()
    }


_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PoE port switches from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        NetgearPoePortSwitch(coordinator, entry, port)
        for port in sorted(coordinator.data.ports)
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_PORT_NAME,
        {vol.Required(ATTR_NAME): cv.string},
        "async_set_port_name",
    )
    platform.async_register_entity_service(
        SERVICE_GET_VLAN_MEMBERSHIP,
        {vol.Required(ATTR_VLAN): _VLAN_ID},
        "async_get_vlan_membership",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_SET_VLAN_MEMBERSHIP,
        {
            vol.Required(ATTR_VLAN): _VLAN_ID,
            vol.Required(ATTR_MEMBERSHIP): vol.In(VLAN_MEMBERSHIP_STATES),
        },
        "async_set_vlan_membership",
    )
    platform.async_register_entity_service(
        SERVICE_SET_PORT_SPEED,
        {
            vol.Required(ATTR_SPEED): vol.In(PORT_SPEEDS),
            vol.Optional(ATTR_DUPLEX, default="auto"): vol.In(PORT_DUPLEXES),
            vol.Optional(ATTR_AUTONEG, default=True): cv.boolean,
            vol.Optional(ATTR_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=52)),
        },
        "async_set_port_speed",
    )


class NetgearPoePortSwitch(NetgearPoePortEntity, SwitchEntity):
    """Switch to enable/disable PoE power on a port."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _name_suffix = "PoE"

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
        port: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, port)
        self._attr_unique_id = f"{entry.entry_id}_poe_port_{port}"

    @property
    def is_on(self) -> bool | None:
        """Return True if PoE is enabled on this port."""
        port_data = self.port_data
        return port_data.admin_enabled if port_data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the detection status and power draw of the port."""
        port_data = self.port_data
        if port_data is None:
            return {}
        return {
            "detection_status": port_data.detection_status,
            "power_watts": port_data.power_watts,
            "link_up": self.coordinator.data.link.get(self._port),
            "port": self._port,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable PoE power on this port."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable PoE power on this port."""
        await self._async_set_enabled(False)

    async def async_set_port_name(self, name: str) -> None:
        """Set the port's description on the switch (set_port_name action).

        Entity names include the description and are rebuilt on each state
        write, so they follow the rename from the next poll. The entity_id
        was generated when the entity was first added and does not change.
        """
        try:
            await self.coordinator.api.async_set_port_name(self._port, name)
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_port_name_failed",
                translation_placeholders={
                    "port": str(self._port),
                    "host": self.coordinator.api.host,
                    "error": str(err),
                },
            ) from err
        port_data = self.port_data
        if port_data is not None:
            port_data.alias = name
        await self.coordinator.async_request_refresh()

    async def async_get_vlan_membership(self, vlan: int) -> dict[str, Any]:
        """Read a VLAN's membership map (get_vlan_membership action).

        Targeting any port entity of the switch returns the whole VLAN's
        map; "port_membership" is the targeted port's own state.
        """
        self._require_vlan_support()
        try:
            membership = await self.coordinator.api.async_get_vlan_membership(vlan)
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vlan_membership_failed",
                translation_placeholders={
                    "vlan": str(vlan),
                    "host": self.coordinator.api.host,
                    "error": str(err),
                },
            ) from err
        return {
            "vlan": membership.vid,
            "name": membership.name,
            "port_membership": _membership_names(membership.ports).get(self._port),
            "ports": _membership_names(membership.ports),
            "lags": _membership_names(membership.lags),
            "configured_vlans": membership.vlans,
        }

    async def async_set_vlan_membership(self, vlan: int, membership: str) -> None:
        """Set this port's membership in a VLAN (set_vlan_membership action).

        Read-modify-write of the full map on the switch; every other port's
        membership and all PVIDs are preserved.
        """
        self._require_vlan_support()
        try:
            await self.coordinator.api.async_set_vlan_port_membership(
                vlan, self._port, _MEMBERSHIP_TO_STATE[membership]
            )
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vlan_membership_failed",
                translation_placeholders={
                    "vlan": str(vlan),
                    "host": self.coordinator.api.host,
                    "error": str(err),
                },
            ) from err

    async def async_set_port_speed(
        self,
        speed: str,
        duplex: str = "auto",
        autoneg: bool = True,
        port: int | None = None,
    ) -> None:
        """Set a port's link speed/duplex (set_port_speed action).

        With autonegotiation on (the default), a specific speed restricts
        what the port advertises and the peer follows — the safe way to pin
        a link's rate. Turning autonegotiation off hard-forces the values.

        Defaults to the targeted entity's own port; `port` redirects the
        change to another port of the same switch. That is how ports with
        no entity of their own — the SFP uplinks, which are never PoE
        ports — get configured: target any port entity of the switch and
        name the SFP port explicitly.
        """
        target_port = port if port is not None else self._port
        if not getattr(self.coordinator.api, "supports_port_speed", False):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="port_speed_not_supported",
                translation_placeholders={"host": self.coordinator.api.host},
            )
        try:
            await self.coordinator.api.async_set_port_speed(
                target_port, speed.removesuffix("M"), duplex, autoneg
            )
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_port_speed_failed",
                translation_placeholders={
                    "port": str(target_port),
                    "host": self.coordinator.api.host,
                    "error": str(err),
                },
            ) from err

    def _require_vlan_support(self) -> None:
        """Raise a clear error if this switch's backend can't do VLANs."""
        if not getattr(self.coordinator.api, "supports_vlan_membership", False):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vlan_membership_not_supported",
                translation_placeholders={"host": self.coordinator.api.host},
            )

    async def _async_set_enabled(self, enabled: bool) -> None:
        try:
            await self.coordinator.api.async_set_port_enabled(self._port, enabled)
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_port_failed",
                translation_placeholders={
                    "port": str(self._port),
                    "host": self.coordinator.api.host,
                    "error": str(err),
                },
            ) from err
        port_data = self.port_data
        if port_data is not None:
            port_data.admin_enabled = enabled
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
