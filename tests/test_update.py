"""Tests for the firmware update platform."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError
from custom_components.netgear_poe.const import FirmwareRelease
from custom_components.netgear_poe.update import _extract_image, resolve_release

from .conftest import MOCK_FIRMWARE, MOCK_MODEL, setup_integration

UPDATE_ENTITY = "update.boiler_switch_firmware"
GS110TP_OID = "1.3.6.1.4.1.4526.100.4.19"

NEW_RELEASE = FirmwareRelease(
    version="9.9.9.9",
    url="http://downloads.example.com/fw_V9.9.9.9.zip",
    notes_url="http://kb.example.com/9999",
)


def _zip_with_stk(image: bytes) -> bytes:
    """Build a Netgear-style zip: the .stk image plus release notes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("fw_V9.9.9.9_Release_Notes.html", "<html>notes</html>")
        archive.writestr("fw_V9.9.9.9.stk", image)
    return buffer.getvalue()


def _download_session(body: bytes) -> MagicMock:
    """A fake HA clientsession serving `body` for any GET.

    Not aioclient_mock: that builds a real ClientSession, whose default
    connector spawns a pycares resolver thread the test harness then flags
    as lingering.
    """
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def test_resolve_unknown_model_is_none() -> None:
    """A model absent from the map offers nothing (no false alarm)."""
    assert resolve_release("6.0.1.16", "9.9.9.9.9", "mystery") is None


def test_resolve_known_newer() -> None:
    """A known model on old firmware surfaces the bundled release."""
    release = resolve_release("5.4.2.30", GS110TP_OID, "GS110TP")
    assert release is not None
    assert release.version == "5.4.2.35"
    assert release.url and release.url.endswith(".zip")
    assert release.notes_url and "kb.netgear.com" in release.notes_url


def test_resolve_up_to_date_is_none() -> None:
    """Matching installed and bundled versions offer nothing."""
    release = resolve_release("5.4.2.30", GS110TP_OID, "x")
    assert release is not None
    assert resolve_release(release.version, GS110TP_OID, "x") is None


def test_resolve_never_downgrades() -> None:
    """A device newer than the map is not offered an older version."""
    assert resolve_release("5.4.2.40", GS110TP_OID, "x") is None


def test_resolve_falls_back_to_model_key() -> None:
    """When the sysObjectID is unknown, the model name is tried."""
    with patch.dict(
        "custom_components.netgear_poe.const.LATEST_FIRMWARE",
        {"GS728TPv2": FirmwareRelease(version="7.0.0.0")},
        clear=False,
    ):
        release = resolve_release("6.0.0.0", "", "GS728TPv2")
    assert release is not None
    assert release.version == "7.0.0.0"


def test_extract_image_from_zip() -> None:
    """The .stk inside Netgear's zip is picked over the release notes."""
    filename, image = _extract_image(_zip_with_stk(b"IMAGE"), "http://x/fw.zip")
    assert filename == "fw_V9.9.9.9.stk"
    assert image == b"IMAGE"


def test_extract_image_passes_through_raw_stk() -> None:
    """A download that is not a zip is assumed to be the image itself."""
    filename, image = _extract_image(b"raw-stk-bytes", "http://x/fw_V1.stk")
    assert filename == "fw_V1.stk"
    assert image == b"raw-stk-bytes"


def test_extract_image_rejects_zip_without_stk() -> None:
    """A zip with no .stk member is an error, not a silent flash of junk."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.html", "x")
    with pytest.raises(HomeAssistantError, match=r"No \.stk"):
        _extract_image(buffer.getvalue(), "http://x/fw.zip")


async def test_update_entity_up_to_date(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The entity reports the installed firmware and no update by default."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(UPDATE_ENTITY)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["installed_version"] == MOCK_FIRMWARE
    assert state.attributes["latest_version"] == MOCK_FIRMWARE


async def test_update_entity_available_without_install(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A newer release shows as available; no Install on this backend."""
    with patch.dict(
        "custom_components.netgear_poe.const.LATEST_FIRMWARE",
        {MOCK_MODEL: NEW_RELEASE},
        clear=False,
    ):
        await setup_integration(hass, mock_config_entry)

    state = hass.states.get(UPDATE_ENTITY)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["installed_version"] == MOCK_FIRMWARE
    assert state.attributes["latest_version"] == "9.9.9.9"
    assert state.attributes["release_url"] == NEW_RELEASE.notes_url
    # The mocked backend models the JSON CGI API, which cannot flash.
    assert not state.attributes["supported_features"] & UpdateEntityFeature.INSTALL


async def test_update_entity_installs(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Install downloads the zip and hands the .stk to the switch API."""
    mock_api.supports_firmware_install = True
    mock_api.async_install_firmware = AsyncMock()
    session = _download_session(_zip_with_stk(b"IMAGE"))

    with (
        patch.dict(
            "custom_components.netgear_poe.const.LATEST_FIRMWARE",
            {MOCK_MODEL: NEW_RELEASE},
            clear=False,
        ),
        patch(
            "custom_components.netgear_poe.update.async_get_clientsession",
            return_value=session,
        ),
    ):
        await setup_integration(hass, mock_config_entry)

        state = hass.states.get(UPDATE_ENTITY)
        assert state is not None
        assert state.attributes["supported_features"] & UpdateEntityFeature.INSTALL

        await hass.services.async_call(
            "update",
            "install",
            {"entity_id": UPDATE_ENTITY},
            blocking=True,
        )

    assert session.get.call_args.args[0] == NEW_RELEASE.url

    mock_api.async_install_firmware.assert_awaited_once()
    args, kwargs = mock_api.async_install_firmware.await_args
    assert args == (b"IMAGE", "9.9.9.9")
    assert kwargs["filename"] == "fw_V9.9.9.9.stk"

    state = hass.states.get(UPDATE_ENTITY)
    assert state.state == "off"
    assert state.attributes["installed_version"] == "9.9.9.9"
    assert state.attributes["in_progress"] is False


async def test_update_entity_install_failure_surfaces(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A switch-side failure raises and leaves the installed version alone."""
    mock_api.supports_firmware_install = True
    mock_api.async_install_firmware = AsyncMock(
        side_effect=NetgearError("upload rejected")
    )

    with (
        patch.dict(
            "custom_components.netgear_poe.const.LATEST_FIRMWARE",
            {MOCK_MODEL: NEW_RELEASE},
            clear=False,
        ),
        patch(
            "custom_components.netgear_poe.update.async_get_clientsession",
            return_value=_download_session(_zip_with_stk(b"IMAGE")),
        ),
    ):
        await setup_integration(hass, mock_config_entry)

        with pytest.raises(HomeAssistantError, match="upload rejected"):
            await hass.services.async_call(
                "update",
                "install",
                {"entity_id": UPDATE_ENTITY},
                blocking=True,
            )

    state = hass.states.get(UPDATE_ENTITY)
    assert state.state == "on"
    assert state.attributes["installed_version"] == MOCK_FIRMWARE
    assert state.attributes["in_progress"] is False
