"""Netgear PoE Switch integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetgearAuthError, NetgearError, NetgearPoeApi, PoeData
from .const import DOMAIN, PLATFORMS, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


@dataclass
class NetgearPoeRuntimeData:
    """Runtime data for the Netgear PoE integration."""

    api: NetgearPoeApi
    coordinator: NetgearPoeCoordinator
    sys_name: str
    model: str


type NetgearPoeConfigEntry = ConfigEntry[NetgearPoeRuntimeData]


class NetgearPoeCoordinator(DataUpdateCoordinator[PoeData]):
    """Coordinator polling PoE state over the switch's web API."""

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
        except NetgearAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NetgearError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"host": self.api.host, "error": str(err)},
            ) from err


async def async_setup_entry(hass: HomeAssistant, entry: NetgearPoeConfigEntry) -> bool:
    """Set up Netgear PoE Switch from a config entry."""
    api = NetgearPoeApi(
        host=entry.data[CONF_HOST],
        password=entry.data[CONF_PASSWORD],
    )

    try:
        sys_name, model = await api.async_get_info()
        await api.async_login()
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

    coordinator = NetgearPoeCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = NetgearPoeRuntimeData(
        api=api,
        coordinator=coordinator,
        sys_name=sys_name,
        model=model,
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
