"""Update platform exposing the switch's firmware version.

Detect-and-notify only: it reports whether a newer firmware is known for the
model (from the hand-maintained LATEST_FIRMWARE map) but does not flash the
switch — the web-UI upload is left to a human.
"""

from __future__ import annotations

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry
from .const import LATEST_FIRMWARE
from .entity import NetgearPoeEntity


def resolve_latest_firmware(
    installed: str, sys_object_id: str, model: str
) -> str | None:
    """Return the newest firmware we know of, or the installed version.

    Never advertises a downgrade: if the bundled "latest" is older than what is
    running (i.e. the device is newer than our map), report the installed
    version so the entity reads "up to date". An unknown model does the same.
    """
    candidate = LATEST_FIRMWARE.get(sys_object_id) or LATEST_FIRMWARE.get(model)
    if not candidate:
        return installed or None
    if not installed:
        return candidate
    try:
        if AwesomeVersion(candidate) <= AwesomeVersion(installed):
            return installed
    except AwesomeVersionException:
        # Unparseable versions: don't risk a false alarm.
        return installed
    return candidate


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware update entity from a config entry."""
    async_add_entities([NetgearPoeFirmwareUpdate(entry)])


class NetgearPoeFirmwareUpdate(NetgearPoeEntity, UpdateEntity):
    """Reports the switch's firmware version and whether an update is known."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(self, entry: NetgearPoeConfigEntry) -> None:
        """Initialize."""
        runtime = entry.runtime_data
        super().__init__(runtime.coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_firmware"
        self._attr_installed_version = runtime.firmware or None
        self._attr_latest_version = resolve_latest_firmware(
            runtime.firmware, runtime.sys_object_id, runtime.model
        )
