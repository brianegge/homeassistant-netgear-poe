"""Button platform for power-cycling Netgear PoE ports."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .api import NetgearError
from .const import DOMAIN
from .entity import NetgearPoePortEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PoE power-cycle buttons from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        NetgearPoePowerCycleButton(coordinator, entry, port)
        for port in sorted(coordinator.data.ports)
    )


class NetgearPoePowerCycleButton(NetgearPoePortEntity, ButtonEntity):
    """Button that power cycles a PoE port via the switch's native reset."""

    _attr_icon = "mdi:restart"

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
        port: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, port)
        self._attr_unique_id = f"{entry.entry_id}_poe_port_{port}_power_cycle"
        self._attr_name = f"{self._port_label()} power cycle"

    async def async_press(self) -> None:
        """Power cycle the port."""
        api = self.coordinator.api
        _LOGGER.info("Power cycling PoE port %d on %s", self._port, api.host)
        try:
            await api.async_power_cycle_port(self._port)
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="power_cycle_failed",
                translation_placeholders={
                    "port": str(self._port),
                    "host": api.host,
                    "error": str(err),
                },
            ) from err
        await self.coordinator.async_request_refresh()
