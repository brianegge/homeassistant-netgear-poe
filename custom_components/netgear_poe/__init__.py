"""Netgear PoE Switch integration."""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetgearAuthError, NetgearError, PoeData
from .api_legacy import NetgearAnyApi, async_detect_api
from .const import (
    CONF_COMMUNITY,
    CONF_ENABLE_TRAPS,
    DISCOVERY_INTERVAL_SECONDS,
    DISCOVERY_SCAN_SECONDS,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL_SECONDS,
)
from .nsdp import async_discover
from .snmp import SnmpLinkMonitor
from .trap_receiver import SnmpTrapReceiver

_LOGGER = logging.getLogger(__name__)
_DISCOVERY_STARTED = f"{DOMAIN}_discovery_started"


@dataclass
class NetgearPoeRuntimeData:
    """Runtime data for the Netgear PoE integration."""

    api: NetgearAnyApi
    coordinator: NetgearPoeCoordinator
    sys_name: str
    model: str
    firmware: str
    sys_object_id: str
    link_monitor: SnmpLinkMonitor | None
    trap_receiver: SnmpTrapReceiver | None


type NetgearPoeConfigEntry = ConfigEntry[NetgearPoeRuntimeData]


class NetgearPoeCoordinator(DataUpdateCoordinator[PoeData]):
    """Coordinator polling PoE state over the switch's web API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: NetgearAnyApi,
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
            data.link, names = await self.link_monitor.async_get_port_info()
            # SNMP ifAlias is the same source LibreNMS uses and needs no web
            # login, so prefer it for port names when available.
            for port, name in names.items():
                if port in data.ports:
                    data.ports[port].alias = name
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
    api: NetgearAnyApi,
    community: str,
    coordinator: NetgearPoeCoordinator,
) -> SnmpTrapReceiver | None:
    """Start the trap receiver and register HA as a destination. Best-effort."""
    source_ip = await hass.async_add_executor_job(_get_source_ip, entry.data[CONF_HOST])
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


async def _async_run_discovery(hass: HomeAssistant) -> None:
    """Run one NSDP scan and offer any new Pro switches for setup."""
    configured = {
        entry.unique_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.unique_id
    }
    try:
        switches = await async_discover(duration=DISCOVERY_SCAN_SECONDS)
    except Exception:
        _LOGGER.debug("NSDP discovery scan failed", exc_info=True)
        return
    _LOGGER.debug(
        "NSDP scan found %d switch(es): %s",
        len(switches),
        ", ".join(f"{s.name}/{s.model}" for s in switches),
    )
    for switch in switches:
        # Only offer switches this integration's web API can actually drive.
        if not switch.is_pro or format_mac(switch.mac) in configured:
            continue
        _LOGGER.info(
            "NSDP discovered %s (%s) at %s — offering setup",
            switch.name or switch.mac,
            switch.model,
            switch.host,
        )
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={
                CONF_HOST: switch.host,
                "mac": switch.mac,
                "model": switch.model,
                "name": switch.name,
            },
        )


@callback
def _async_start_discovery(hass: HomeAssistant) -> None:
    """Start the periodic NSDP discovery scan once per HA run."""
    if hass.data.get(_DISCOVERY_STARTED):
        return
    hass.data[_DISCOVERY_STARTED] = True

    async def _scan_loop() -> None:
        while True:
            await _async_run_discovery(hass)
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)

    hass.async_create_background_task(_scan_loop(), f"{DOMAIN}_discovery")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Enable NSDP discovery when the integration is listed in configuration.yaml."""

    @callback
    def _start(_event: Event) -> None:
        _async_start_discovery(hass)

    # Defer the first scan until HA has started so it doesn't slow startup.
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NetgearPoeConfigEntry) -> bool:
    """Set up Netgear PoE Switch from a config entry."""
    # Once any switch is configured, keep scanning to surface the others.
    if hass.is_running:
        _async_start_discovery(hass)
    else:
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, lambda _e: _async_start_discovery(hass)
        )

    try:
        api = await async_detect_api(
            host=entry.data[CONF_HOST],
            password=entry.data[CONF_PASSWORD],
        )
    except NetgearError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="connection_failed",
            translation_placeholders={"host": entry.data[CONF_HOST], "error": str(err)},
        ) from err

    try:
        info = await api.async_get_info()
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
    link_monitor = (
        SnmpLinkMonitor(entry.data[CONF_HOST], community) if community else None
    )
    # With SNMP present, take port names from ifAlias instead of the web CGI.
    api.web_port_names_enabled = link_monitor is None

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
        sys_name=info.name,
        model=info.model,
        firmware=info.firmware,
        sys_object_id=info.sys_object_id,
        link_monitor=link_monitor,
        trap_receiver=trap_receiver,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NetgearPoeConfigEntry) -> bool:
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
