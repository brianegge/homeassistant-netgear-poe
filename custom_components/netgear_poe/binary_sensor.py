"""Binary sensor platform for Netgear switch port link state (via SNMP)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .entity import NetgearPoePortEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up port link sensors when SNMP is configured."""
    if entry.runtime_data.link_monitor is None:
        return
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        NetgearPortLinkSensor(coordinator, entry, port)
        for port in sorted(coordinator.data.ports)
    )


class NetgearPortLinkSensor(NetgearPoePortEntity, BinarySensorEntity):
    """Link state of a switch port, read via SNMP ifOperStatus."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
        port: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, port)
        self._attr_unique_id = f"{entry.entry_id}_port_{port}_link"
        self._attr_name = f"{self._port_label()} link"

    @property
    def available(self) -> bool:
        """Unavailable while SNMP has no data (e.g. hung agent)."""
        return super().available and self._port in self.coordinator.data.link

    @property
    def is_on(self) -> bool | None:
        """Return True if the port has link."""
        return self.coordinator.data.link.get(self._port)
