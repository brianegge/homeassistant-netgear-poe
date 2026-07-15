"""Update platform exposing — and installing — the switch's firmware.

Whether a newer firmware is known comes from the hand-maintained
LATEST_FIRMWARE map. On backends that can flash (the classic /base/ web UI),
the entity also offers Install: it downloads Netgear's zip, extracts the .stk
image, uploads it to the switch's inactive firmware slot, activates it and
reboots. The reboot drops PoE — every powered camera and AP — and the
switch's own uplink for around a minute, and the previously running firmware
stays flashed in the other slot as a rollback.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetgearPoeConfigEntry
from .api import NetgearError
from .const import LATEST_FIRMWARE, FirmwareRelease
from .entity import NetgearPoeEntity

_DOWNLOAD_TIMEOUT = 120


def resolve_release(
    installed: str, sys_object_id: str, model: str
) -> FirmwareRelease | None:
    """Return the bundled release if it is newer than what is running.

    None means nothing newer is known: the model is absent from the map, the
    versions match, or the device is ahead of the map (never advertise a
    downgrade). Unparseable versions also return None rather than risk a
    false alarm.
    """
    release = LATEST_FIRMWARE.get(sys_object_id) or LATEST_FIRMWARE.get(model)
    if release is None:
        return None
    if not installed:
        return release
    try:
        if AwesomeVersion(release.version) <= AwesomeVersion(installed):
            return None
    except AwesomeVersionException:
        return None
    return release


# The flashable member of Netgear's zip: .stk on the classic /base/ models,
# .ros on the xui ones. The rest of the zip is release notes.
_IMAGE_SUFFIXES = (".stk", ".ros")


def _extract_image(blob: bytes, url: str) -> tuple[str, bytes]:
    """Return (filename, image) from a download; Netgear zips the image.

    Only a URL that names the image itself may pass its body through
    unwrapped. Anything else must be a real zip: a proxy or CDN error page
    served with HTTP 200 for a .zip URL would otherwise be handed to the
    switch and overwrite the rollback slot with junk.
    """
    name = url.rsplit("/", 1)[-1]
    if blob.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for member in archive.namelist():
                if member.lower().endswith(_IMAGE_SUFFIXES):
                    return member.rsplit("/", 1)[-1], archive.read(member)
        raise HomeAssistantError(
            "No firmware image ("
            + "/".join(_IMAGE_SUFFIXES)
            + ") in the downloaded archive"
        )
    if name.lower().endswith(_IMAGE_SUFFIXES):
        return name, blob
    raise HomeAssistantError(
        f"{url} did not return a firmware image (got {len(blob)} bytes that "
        "are neither a zip nor a named image)"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware update entity from a config entry."""
    async_add_entities([NetgearPoeFirmwareUpdate(entry)])


class NetgearPoeFirmwareUpdate(NetgearPoeEntity, UpdateEntity):
    """Reports the switch's firmware version and installs known updates."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(self, entry: NetgearPoeConfigEntry) -> None:
        """Initialize."""
        runtime = entry.runtime_data
        super().__init__(runtime.coordinator, entry)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_firmware"
        self._attr_installed_version = runtime.firmware or None
        self._release = resolve_release(
            runtime.firmware, runtime.sys_object_id, runtime.model
        )
        if self._release is None:
            self._attr_latest_version = runtime.firmware or None
            return
        self._attr_latest_version = self._release.version
        self._attr_release_url = self._release.notes_url
        if self._release.url and getattr(
            runtime.api, "supports_firmware_install", False
        ):
            self._attr_supported_features = (
                UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
            )

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Download the release and install it on the switch.

        The switch reboots at the end, dropping PoE (cameras, APs) and its
        own network link for around a minute.
        """
        release = self._release
        if release is None or not release.url:
            raise HomeAssistantError("No downloadable firmware known for this switch")
        self._report_progress(1)
        try:
            filename, image = await self._async_download(release.url)
            self._report_progress(10)
            await self._entry.runtime_data.api.async_install_firmware(
                image,
                release.version,
                filename=filename,
                progress=self._report_progress,
            )
        except NetgearError as err:
            raise HomeAssistantError(f"Firmware install failed: {err}") from err
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()
        self._note_installed(release.version)
        await self.coordinator.async_request_refresh()

    async def _async_download(self, url: str) -> tuple[str, bytes]:
        """Fetch the release and return the image to flash."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)
            ) as resp:
                resp.raise_for_status()
                blob = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Could not download firmware: {err}") from err
        return await self.hass.async_add_executor_job(_extract_image, blob, url)

    @callback
    def _report_progress(self, percent: int) -> None:
        """Surface install progress on the entity."""
        self._attr_in_progress = True
        self._attr_update_percentage = percent
        self.async_write_ha_state()

    @callback
    def _note_installed(self, version: str) -> None:
        """Record the new firmware everywhere the old one was shown."""
        self._attr_installed_version = version
        self._entry.runtime_data.firmware = version
        self.async_write_ha_state()
        if self.device_entry is not None:
            dr.async_get(self.hass).async_update_device(
                self.device_entry.id, sw_version=version
            )
