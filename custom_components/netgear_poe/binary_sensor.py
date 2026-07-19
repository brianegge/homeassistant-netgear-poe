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
from .entity import NetgearPoeEntity, NetgearPoePortEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PoE-controller problem sensor and, with SNMP, link sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = [NetgearPoeStalledSensor(coordinator, entry)]
    if entry.runtime_data.link_monitor is not None:
        entities.extend(
            NetgearPortLinkSensor(coordinator, entry, port)
            for port in sorted(coordinator.data.ports)
        )
    async_add_entities(entities)


class NetgearPoeStalledSensor(NetgearPoeEntity, BinarySensorEntity):
    """Problem sensor: PoE telemetry stalled while the switch stays reachable.

    Turns on when the switch keeps answering management reads but its PoE
    status query hangs — a wedged PoE controller that a cold power-cycle
    clears (see the coordinator's stall handling). Power delivery is
    unaffected, so this flags "a cold reboot is needed", not "PoE is down".
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "poe_controller_stalled"

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_poe_controller_stalled"

    @property
    def is_on(self) -> bool:
        """Return True while the PoE controller telemetry is stalled."""
        return self.coordinator.poe_stalled


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
