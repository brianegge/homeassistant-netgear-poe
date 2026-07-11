"""Switch platform for Netgear PoE ports."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .api import SnmpError
from .const import DOMAIN
from .entity import NetgearPoePortEntity

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


class NetgearPoePortSwitch(NetgearPoePortEntity, SwitchEntity):
    """Switch to enable/disable PoE power on a port."""

    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
        port: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, port)
        self._attr_unique_id = f"{entry.entry_id}_poe_port_{port}"
        self._attr_name = f"{self._port_label()} PoE"

    @property
    def is_on(self) -> bool | None:
        """Return True if PoE is enabled on this port."""
        port_data = self.port_data
        return port_data.admin_enabled if port_data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the detection status of the port."""
        port_data = self.port_data
        if port_data is None:
            return {}
        return {
            "detection_status": port_data.detection_status,
            "port": self._port,
            "alias": port_data.alias,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable PoE power on this port."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable PoE power on this port."""
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        try:
            await self.coordinator.api.async_set_port_enabled(self._port, enabled)
        except SnmpError as err:
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
