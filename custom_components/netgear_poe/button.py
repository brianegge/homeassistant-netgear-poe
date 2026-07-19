"""Button platform for power-cycling Netgear PoE ports."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .api import NetgearError
from .const import DOMAIN
from .entity import NetgearPoeEntity, NetgearPoePortEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the reboot button and per-port PoE power-cycle buttons."""
    coordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = [NetgearRebootButton(coordinator, entry)]
    entities.extend(
        NetgearPoePowerCycleButton(coordinator, entry, port)
        for port in sorted(coordinator.data.ports)
    )
    async_add_entities(entities)


class NetgearRebootButton(NetgearPoeEntity, ButtonEntity):
    """Button that reboots the whole switch via its web UI.

    The recovery for a wedged PoE controller or a hung SNMP agent. A reboot
    drops PoE — every powered camera and AP — and the switch's own uplink for
    a minute or more, so it is a deliberate, disruptive action.
    """

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "reboot"

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reboot"

    async def async_press(self) -> None:
        """Reboot the switch."""
        api = self.coordinator.api
        _LOGGER.info("Rebooting switch %s", api.host)
        try:
            await api.async_reboot()
        except NetgearError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reboot_failed",
                translation_placeholders={"host": api.host, "error": str(err)},
            ) from err


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
