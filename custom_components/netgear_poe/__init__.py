"""Netgear PoE Switch integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetgearPoeApi, PoeData, SnmpError
from .const import (
    CONF_COMMUNITY,
    CONF_WRITE_COMMUNITY,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class NetgearPoeRuntimeData:
    """Runtime data for the Netgear PoE integration."""

    api: NetgearPoeApi
    coordinator: NetgearPoeCoordinator
    sys_name: str
    sys_descr: str


type NetgearPoeConfigEntry = ConfigEntry[NetgearPoeRuntimeData]


class NetgearPoeCoordinator(DataUpdateCoordinator[PoeData]):
    """Coordinator polling PoE state over SNMP."""

    def __init__(self, hass: HomeAssistant, api: NetgearPoeApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Netgear PoE ({api.host})",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.api = api

    async def _async_update_data(self) -> PoeData:
        try:
            return await self.api.async_get_data()
        except SnmpError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"host": self.api.host, "error": str(err)},
            ) from err


async def async_setup_entry(hass: HomeAssistant, entry: NetgearPoeConfigEntry) -> bool:
    """Set up Netgear PoE Switch from a config entry."""
    api = NetgearPoeApi(
        host=entry.data[CONF_HOST],
        community=entry.data[CONF_COMMUNITY],
        write_community=entry.data.get(CONF_WRITE_COMMUNITY, ""),
    )

    try:
        sys_name, sys_descr = await api.async_get_info()
    except SnmpError as err:
        await api.async_close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="connection_failed",
            translation_placeholders={"host": entry.data[CONF_HOST], "error": str(err)},
        ) from err

    coordinator = NetgearPoeCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = NetgearPoeRuntimeData(
        api=api,
        coordinator=coordinator,
        sys_name=sys_name,
        sys_descr=sys_descr,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NetgearPoeConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.api.async_close()
    return unload_ok
