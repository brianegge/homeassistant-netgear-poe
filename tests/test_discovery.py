"""Tests for NSDP discovery: parsing, the flow, and the scanner."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.const import DOMAIN
from custom_components.netgear_poe.nsdp import (
    NsdpSwitch,
    _build_request,
    _parse_response,
)

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
        user_input={"password": "test-password", "community": "test-community"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == MOCK_SYS_NAME
    assert result["data"]["host"] == "192.168.254.250"
    assert result["data"]["password"] == "test-password"


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


def _ssdp_info(host: str, mac_hex: str) -> SsdpServiceInfo:
    """An SsdpServiceInfo shaped like a real GS728TPPv3 announcement."""
    return SsdpServiceInfo(
        ssdp_usn=f"uuid:fb8e67ba-7dba-11e7-be55-{mac_hex}::"
        "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
        ssdp_st="urn:schemas-upnp-org:device:InternetGatewayDevice:1",
        ssdp_location=f"http://{host}:44291/rootDesc.xml",  # NOSONAR — mock
        upnp={
            "UDN": f"uuid:fb8e67ba-7dba-11e7-be55-{mac_hex}",
            "friendlyName": "GS728TPPv3-BBE5C2",
            "manufacturer": "NETGEAR",
            "modelDescription": "NETGEAR Switch",
            "modelName": "GS728TPPv3",
            "presentationURL": f"http://{host}/",  # NOSONAR — mock
        },
    )


async def test_ssdp_flow_creates_entry(
    hass: HomeAssistant, mock_api: MagicMock
) -> None:
    """An SSDP-announced switch (no NSDP, like the GS728TPPv3) can be set up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=_ssdp_info("192.168.254.250", "289401bbe5c2"),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"password": "test-password"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "192.168.254.250"

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "28:94:01:bb:e5:c2"


async def test_ssdp_flow_dedupes_configured(hass: HomeAssistant) -> None:
    """An SSDP announcement for a configured switch aborts (and updates host)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.168.254.9", "password": "x", "community": ""},
        unique_id="28:94:01:bb:e5:c2",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=_ssdp_info("192.168.254.250", "289401bbe5c2"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The moved switch's new address is picked up.
    assert entry.data["host"] == "192.168.254.250"


async def test_ssdp_flow_rejects_malformed(hass: HomeAssistant) -> None:
    """An announcement without a usable MAC aborts instead of crashing."""
    info = _ssdp_info("192.168.254.250", "289401bbe5c2")
    info.upnp["UDN"] = "uuid:not-a-mac"

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=info,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_scan_addrs_cover_all_interfaces(hass: HomeAssistant) -> None:
    """Each enabled interface contributes its directed broadcast and its IP.

    The global 255.255.255.255 only leaves via the default-route interface,
    so a multi-homed host must also target the other subnets directly and
    bind a socket per interface.
    """
    from custom_components.netgear_poe import _async_scan_addrs

    adapters = [
        {"enabled": True, "ipv4": [{"address": "192.168.3.13", "network_prefix": 24}]},
        {"enabled": True, "ipv4": [{"address": "192.168.1.109", "network_prefix": 24}]},
        # Disabled adapters, loopback, and /32 contribute nothing.
        {"enabled": False, "ipv4": [{"address": "10.0.0.5", "network_prefix": 24}]},
        {"enabled": True, "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}]},
        {"enabled": True, "ipv4": [{"address": "10.1.2.3", "network_prefix": 32}]},
    ]
    with patch(
        "custom_components.netgear_poe.network.async_get_adapters",
        return_value=adapters,
    ):
        broadcast, local = await _async_scan_addrs(hass)

    assert broadcast == ("192.168.1.255", "192.168.3.255", "255.255.255.255")
    assert local == ("192.168.1.109", "192.168.3.13")


async def test_scan_addrs_survive_helper_failure(hass: HomeAssistant) -> None:
    """If adapter enumeration fails, fall back to the global broadcast."""
    from custom_components.netgear_poe import _async_scan_addrs

    with patch(
        "custom_components.netgear_poe.network.async_get_adapters",
        side_effect=RuntimeError("no network integration"),
    ):
        broadcast, local = await _async_scan_addrs(hass)

    assert broadcast == ("255.255.255.255",)
    assert local == ()


async def test_scanner_offers_pro_and_probed_plus_switches(
    hass: HomeAssistant,
) -> None:
    """Pro switches are offered outright; Plus ones only if their UI probes OK."""
    from custom_components.netgear_poe import _async_run_discovery

    switches = [
        NsdpSwitch(
            "28:80:88:54:db:08", "192.168.254.250", "GS728TPv2", "boiler", "6", True
        ),
        # Plus-port switch running a supported base-UI generation.
        NsdpSwitch(
            "3c:37:86:17:47:34", "192.168.254.251", "GS110TP", "chapel", "5", False
        ),
        # Plus-port switch with no drivable web UI.
        NsdpSwitch(
            "28:80:88:e0:07:23", "192.168.254.252", "GS108PEv3", "deck", "2", False
        ),
    ]
    created: list[dict] = []
    probed: list[str] = []

    def fake_probe(host: str) -> bool:
        # A plain function: patch() wraps async_probe_supported in an
        # AsyncMock, which awaits the side_effect's return value itself.
        probed.append(host)
        return host == "192.168.254.251"

    with (
        patch("custom_components.netgear_poe.async_discover", return_value=switches),
        patch(
            "custom_components.netgear_poe.async_probe_supported",
            side_effect=fake_probe,
        ),
        patch(
            "custom_components.netgear_poe.discovery_flow.async_create_flow",
            side_effect=lambda h, d, *, context, data: created.append(data),
        ),
    ):
        await _async_run_discovery(hass)

    # The Pro switch is never probed; both Plus switches are.
    assert probed == ["192.168.254.251", "192.168.254.252"]
    assert [c["host"] for c in created] == ["192.168.254.250", "192.168.254.251"]
    assert created[1]["mac"] == "3c:37:86:17:47:34"


async def test_scanner_skips_probe_for_configured_plus_switch(
    hass: HomeAssistant,
) -> None:
    """An already-configured switch is skipped before any web probe."""
    from custom_components.netgear_poe import _async_run_discovery

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.168.254.251", "password": "x", "community": ""},
        unique_id="3c:37:86:17:47:34",
    )
    entry.add_to_hass(hass)
    switches = [
        NsdpSwitch(
            "3c:37:86:17:47:34", "192.168.254.251", "GS110TP", "chapel", "5", False
        ),
    ]

    with (
        patch("custom_components.netgear_poe.async_discover", return_value=switches),
        patch("custom_components.netgear_poe.async_probe_supported") as probe,
        patch(
            "custom_components.netgear_poe.discovery_flow.async_create_flow"
        ) as create_flow,
    ):
        await _async_run_discovery(hass)

    probe.assert_not_called()
    create_flow.assert_not_called()
