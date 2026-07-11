"""Sensor platform for Netgear PoE power consumption."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .entity import NetgearPoeEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PoE power sensor from a config entry."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.data.consumption_watts is not None:
        async_add_entities([NetgearPoePowerSensor(coordinator, entry)])


class NetgearPoePowerSensor(NetgearPoeEntity, SensorEntity):
    """Total PoE power delivered by the switch."""

    _attr_translation_key = "poe_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self, coordinator: NetgearPoeCoordinator, entry: NetgearPoeConfigEntry
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_poe_power"

    @property
    def native_value(self) -> int | None:
        """Return total PoE consumption in watts."""
        return self.coordinator.data.consumption_watts
