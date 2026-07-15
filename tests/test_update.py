"""Tests for the firmware update platform."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.update import resolve_latest_firmware

from .conftest import MOCK_FIRMWARE, MOCK_MODEL, setup_integration

UPDATE_ENTITY = "update.boiler_switch_firmware"
GS516TP_OID = "1.3.6.1.4.1.4526.100.4.29"


def test_resolve_unknown_model_reports_installed() -> None:
    """A model absent from the map reports the installed version (no alarm)."""
    assert resolve_latest_firmware("6.0.1.16", "9.9.9.9.9", "mystery") == "6.0.1.16"


def test_resolve_known_newer() -> None:
    """A known model on old firmware surfaces the bundled newer version."""
    # GS110TP sysObjectID -> 5.4.2.33 in the bundled map.
    latest = resolve_latest_firmware(
        "5.4.2.30", "1.3.6.1.4.1.4526.100.4.19", "GS110TP"
    )
    assert latest == "5.4.2.33"


def test_resolve_up_to_date() -> None:
    """Matching installed and latest versions report as up to date."""
    assert resolve_latest_firmware("6.0.1.16", GS516TP_OID, "x") == "6.0.1.16"


def test_resolve_never_downgrades() -> None:
    """A device newer than the map is not offered an older version."""
    assert resolve_latest_firmware("5.4.2.40", "1.3.6.1.4.1.4526.100.4.19", "x") == (
        "5.4.2.40"
    )


def test_resolve_falls_back_to_model_key() -> None:
    """When the sysObjectID is unknown, the model name is tried."""
    with patch.dict(
        "custom_components.netgear_poe.const.LATEST_FIRMWARE",
        {"GS728TPv2": "7.0.0.0"},
        clear=False,
    ):
        assert resolve_latest_firmware("6.0.0.0", "", "GS728TPv2") == "7.0.0.0"


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


async def test_update_entity_available(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A newer bundled version flips the entity to 'update available'."""
    with patch.dict(
        "custom_components.netgear_poe.const.LATEST_FIRMWARE",
        {MOCK_MODEL: "9.9.9.9"},
        clear=False,
    ):
        await setup_integration(hass, mock_config_entry)

    state = hass.states.get(UPDATE_ENTITY)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["installed_version"] == MOCK_FIRMWARE
    assert state.attributes["latest_version"] == "9.9.9.9"
