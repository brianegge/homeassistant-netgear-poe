"""Netgear PoE Switch integration."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetgearAuthError, NetgearError, NetgearPoeApi, PoeData
from .const import (
    CONF_COMMUNITY,
    CONF_ENABLE_TRAPS,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL_SECONDS,
)
from .snmp import SnmpLinkMonitor
from .trap_receiver import SnmpTrapReceiver

_LOGGER = logging.getLogger(__name__)


@dataclass
class NetgearPoeRuntimeData:
    """Runtime data for the Netgear PoE integration."""

    api: NetgearPoeApi
    coordinator: NetgearPoeCoordinator
    sys_name: str
    model: str
    link_monitor: SnmpLinkMonitor | None
    trap_receiver: SnmpTrapReceiver | None


type NetgearPoeConfigEntry = ConfigEntry[NetgearPoeRuntimeData]


class NetgearPoeCoordinator(DataUpdateCoordinator[PoeData]):
    """Coordinator polling PoE state over the switch's web API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: NetgearPoeApi,
        link_monitor: SnmpLinkMonitor | None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Netgear PoE ({api.host})",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.api = api
        self.link_monitor = link_monitor

    async def _async_update_data(self) -> PoeData:
        try:
            data = await self.api.async_get_data()
        except NetgearAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NetgearError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"host": self.api.host, "error": str(err)},
            ) from err
        if self.link_monitor is not None:
            data.link = await self.link_monitor.async_get_link_states()
        return data

    @callback
    def apply_link_trap(self, port: int, up: bool) -> None:
        """Apply an out-of-band link change from a trap and notify entities."""
        if self.data is None:
            return
        if self.data.link.get(port) == up:
            return
        self.data.link[port] = up
        self.async_update_listeners()
        # Reconcile authoritative state (PoE draw etc.) soon after.
        self.hass.async_create_task(self.async_request_refresh())


def _get_source_ip(target_host: str) -> str | None:
    """Return the local IP the OS would use to reach the switch."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_host, 161))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


async def _async_setup_traps(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    api: NetgearPoeApi,
    community: str,
    coordinator: NetgearPoeCoordinator,
) -> SnmpTrapReceiver | None:
    """Start the trap receiver and register HA as a destination. Best-effort."""
    source_ip = await hass.async_add_executor_job(
        _get_source_ip, entry.data[CONF_HOST]
    )
    if not source_ip:
        _LOGGER.warning("Could not determine local IP; SNMP traps disabled")
        return None

    receiver = SnmpTrapReceiver(
        community=community,
        on_link_change=coordinator.apply_link_trap,
    )
    try:
        await receiver.async_start()
    except OSError as err:
        _LOGGER.warning(
            "Could not bind SNMP trap port 162 (%s); falling back to polling", err
        )
        return None

    try:
        await api.async_ensure_trap_destination(source_ip, community)
    except NetgearError as err:
        _LOGGER.warning("Could not register trap destination on switch: %s", err)
    else:
        _LOGGER.info("Registered %s as SNMP trap destination on switch", source_ip)
    return receiver


async def async_setup_entry(hass: HomeAssistant, entry: NetgearPoeConfigEntry) -> bool:
    """Set up Netgear PoE Switch from a config entry."""
    api = NetgearPoeApi(
        host=entry.data[CONF_HOST],
        password=entry.data[CONF_PASSWORD],
    )

    try:
        sys_name, model = await api.async_get_info()
    except NetgearAuthError as err:
        await api.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except NetgearError as err:
        await api.async_close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="connection_failed",
            translation_placeholders={"host": entry.data[CONF_HOST], "error": str(err)},
        ) from err

    community = entry.data.get(CONF_COMMUNITY)
    link_monitor = SnmpLinkMonitor(entry.data[CONF_HOST], community) if community else None

    coordinator = NetgearPoeCoordinator(hass, api, link_monitor)
    await coordinator.async_config_entry_first_refresh()

    trap_receiver: SnmpTrapReceiver | None = None
    if community and entry.data.get(CONF_ENABLE_TRAPS, True):
        trap_receiver = await _async_setup_traps(
            hass, entry, api, community, coordinator
        )

    entry.runtime_data = NetgearPoeRuntimeData(
        api=api,
        coordinator=coordinator,
        sys_name=sys_name,
        model=model,
        link_monitor=link_monitor,
        trap_receiver=trap_receiver,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NetgearPoeConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = entry.runtime_data
        await runtime.api.async_close()
        if runtime.link_monitor is not None:
            await runtime.link_monitor.async_close()
        if runtime.trap_receiver is not None:
            await runtime.trap_receiver.async_stop()
    return unload_ok
