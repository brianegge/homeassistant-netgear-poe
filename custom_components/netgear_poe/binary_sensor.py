"""Binary sensor platform for Netgear switch port link state (via SNMP)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
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
    """Set up the PoE-controller problem sensor and, with SNMP, link sensors.

    Link sensors cover every port SNMP reports link state for, not just the
    PoE ports: a non-PoE port (an uplink, or gear with its own power) is often
    exactly what a link trap is wanted for. Those are created disabled by
    default so a switch with many such ports doesn't flood Home Assistant;
    PoE ports keep their link sensor enabled as before.

    Ports are added as they first appear in the coordinator's data, so a
    switch whose SNMP agent was silent at setup still gets them once it
    answers. A port is only classed non-PoE once PoE data has arrived — before
    that, every port would look non-PoE and be created disabled.
    """
    coordinator = entry.runtime_data.coordinator
    async_add_entities([NetgearPoeStalledSensor(coordinator, entry)])
    if entry.runtime_data.link_monitor is None:
        return

    known: set[int] = set()

    @callback
    def _async_add_link_sensors() -> None:
        data = coordinator.data
        if data is None:
            return
        new: list[NetgearPortLinkSensor] = []
        for port in sorted(set(data.ports) | set(data.link)):
            if port in known or (port not in data.ports and not data.ports):
                continue
            known.add(port)
            new.append(
                NetgearPortLinkSensor(coordinator, entry, port, poe=port in data.ports)
            )
        if new:
            async_add_entities(new)

    _async_add_link_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_link_sensors))


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
    """Link state of a switch port, read via SNMP ifOperStatus.

    Exists for PoE and non-PoE ports alike; the unique_id has no PoE marker,
    so a PoE port's sensor keeps the id it always had.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _name_suffix = "link"

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
        port: int,
        poe: bool = True,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, port)
        self._attr_unique_id = f"{entry.entry_id}_port_{port}_link"
        if not poe:
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        """Unavailable while SNMP has no data (e.g. hung agent).

        Not gated on the port having PoE data, as other per-port entities
        are: a non-PoE port never has any, and link state is SNMP's alone.
        """
        return (
            self.coordinator.last_update_success
            and self._port in self.coordinator.data.link
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the port has link."""
        return self.coordinator.data.link.get(self._port)
