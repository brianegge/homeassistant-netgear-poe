"""Tests for NSDP discovery: parsing, the flow, and the scanner."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.const import DOMAIN
from custom_components.netgear_poe.nsdp import NsdpSwitch, _build_request, _parse_response

from .conftest import MOCK_SYS_NAME

BOILER_MAC = "28:80:88:54:db:08"
DISCOVERY_INFO = {
    "host": "192.168.254.250",
    "mac": BOILER_MAC,
    "model": "GS728TPv2",
    "name": "boiler-switch",
}


def _make_response(mac: str, ip: str, model: str, name: str) -> bytes:
    header = (
        struct.pack("!BBH", 1, 2, 0)  # op 2 = read response
        + b"\x00" * 4
        + b"\x00" * 6
        + bytes(int(x, 16) for x in mac.split(":"))
        + b"\x00" * 2
        + struct.pack("!H", 1)
        + b"NSDP"
        + b"\x00" * 4
    )
    body = b""
    for tag, val in (
        (0x0001, model.encode()),
        (0x0003, name.encode()),
        (0x0004, bytes(int(x, 16) for x in mac.split(":"))),
        (0x0006, bytes(int(o) for o in ip.split("."))),
    ):
        body += struct.pack("!HH", tag, len(val)) + val
    return header + body + struct.pack("!HH", 0xFFFF, 0)


def test_parse_response() -> None:
    """A well-formed response parses into an NsdpSwitch."""
    data = _make_response(BOILER_MAC, "192.168.254.250", "GS728TPv2", "boiler-switch")
    switch = _parse_response(data, is_pro=True)
    assert switch is not None
    assert switch.mac == BOILER_MAC
    assert switch.host == "192.168.254.250"
    assert switch.model == "GS728TPv2"
    assert switch.name == "boiler-switch"
    assert switch.is_pro is True


def test_parse_response_rejects_junk() -> None:
    """Non-NSDP or request packets are ignored."""
    assert _parse_response(b"not an nsdp packet", is_pro=True) is None
    # A request (op 1), not a response, must be rejected.
    assert _parse_response(_build_request(1), is_pro=False) is None


async def test_discovery_flow_creates_entry(
    hass: HomeAssistant, mock_api: MagicMock
) -> None:
    """A discovered switch can be set up after entering the password."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=DISCOVERY_INFO,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"password": "Password1", "community": "egge"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == MOCK_SYS_NAME
    assert result["data"]["host"] == "192.168.254.250"
    assert result["data"]["password"] == "Password1"


async def test_discovery_flow_dedupes_configured(
    hass: HomeAssistant, mock_api: MagicMock
) -> None:
    """A discovered switch already configured (by MAC) aborts."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.168.254.250", "password": "x", "community": ""},
        unique_id="28:80:88:54:db:08",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=DISCOVERY_INFO,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_scanner_offers_only_pro_switches(hass: HomeAssistant) -> None:
    """One scan pass creates flows for Pro switches and skips Plus ones."""
    from custom_components.netgear_poe import _async_run_discovery

    switches = [
        NsdpSwitch(
            "28:80:88:54:db:08", "192.168.254.250", "GS728TPv2", "boiler", "6", True
        ),
        NsdpSwitch(
            "28:80:88:e0:07:23", "192.168.254.252", "GS108PEv3", "deck", "2", False
        ),
    ]
    created: list[dict] = []

    with (
        patch(
            "custom_components.netgear_poe.async_discover", return_value=switches
        ),
        patch(
            "custom_components.netgear_poe.discovery_flow.async_create_flow",
            side_effect=lambda h, d, *, context, data: created.append(data),
        ),
    ):
        await _async_run_discovery(hass)

    assert len(created) == 1
    assert created[0]["host"] == "192.168.254.250"
    assert created[0]["mac"] == "28:80:88:54:db:08"
