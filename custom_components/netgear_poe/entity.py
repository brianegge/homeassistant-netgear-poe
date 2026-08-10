"""Base entity for the Netgear PoE Switch integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NetgearPoeConfigEntry, NetgearPoeCoordinator
from .api import PoePort
from .const import DOMAIN


class NetgearPoeEntity(CoordinatorEntity[NetgearPoeCoordinator]):
    """Base class for Netgear PoE entities."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: NetgearPoeCoordinator, entry: NetgearPoeConfigEntry
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.runtime_data.sys_name or entry.title,
            manufacturer="Netgear",
            model=entry.runtime_data.model or None,
            sw_version=entry.runtime_data.firmware or None,
            configuration_url=(
                f"http{'s' if getattr(coordinator.api, 'use_https', False) else ''}"
                f"://{coordinator.api.host}/"
            ),
        )


class NetgearPoePortEntity(NetgearPoeEntity):
    """Base class for per-port entities.

    Subclasses set `_name_suffix` ("PoE", "link", ...) instead of a fixed
    `_attr_name`: the name is built on every state write so a port that is
    renamed on the switch — or whose name arrives late, after a wedged SNMP
    agent recovers — picks the new label up on the next poll. The entity_id
    is still generated from the name at first add and never changes.
    """

    _name_suffix = ""

    def __init__(
        self,
        coordinator: NetgearPoeCoordinator,
        entry: NetgearPoeConfigEntry,
        port: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry)
        self._port = port

    @property
    def port_data(self) -> PoePort | None:
        """Return the current data for this port."""
        return self.coordinator.data.ports.get(self._port)

    @property
    def available(self) -> bool:
        """Return True if the port is present in the last update."""
        return super().available and self.port_data is not None

    @property
    def name(self) -> str:
        """Return the port label plus this entity's suffix."""
        return f"{self._port_label()} {self._name_suffix}".strip()

    def _port_label(self) -> str:
        """Return a friendly label like 'Port 3 (camera)'."""
        port_data = self.port_data
        if port_data is not None and port_data.alias:
            return f"Port {self._port} ({port_data.alias})"
        return f"Port {self._port}"
