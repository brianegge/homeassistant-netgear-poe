"""Diagnostics support for Netgear PoE Switch."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import NetgearPoeConfigEntry
from .const import CONF_COMMUNITY

TO_REDACT = [CONF_PASSWORD, CONF_COMMUNITY]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NetgearPoeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "sys_name": entry.runtime_data.sys_name,
        "model": entry.runtime_data.model,
        "firmware": entry.runtime_data.firmware,
        "data": asdict(coordinator.data) if coordinator.data else None,
        "last_update_success": coordinator.last_update_success,
    }
