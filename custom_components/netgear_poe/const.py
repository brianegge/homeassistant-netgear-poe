"""Constants for the Netgear PoE Switch integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "netgear_poe"

CONF_COMMUNITY: Final = "community"
CONF_ENABLE_TRAPS: Final = "enable_traps"

SERVICE_SET_PORT_NAME: Final = "set_port_name"

SCAN_INTERVAL_SECONDS: Final = 30

# NSDP discovery: switches answer probabilistically, so each scan runs a while.
DISCOVERY_SCAN_SECONDS: Final = 45
DISCOVERY_INTERVAL_SECONDS: Final = 600

PLATFORMS: Final[list[str]] = [
    "binary_sensor",
    "button",
    "sensor",
    "switch",
    "update",
]

# Latest firmware per model, maintained by hand from Netgear's download pages.
# Keyed by SNMP sysObjectID (model-exact) with the clean model name as a
# fallback key. When a switch matches, HA surfaces an "update available" if it
# is running something older. Bump these as Netgear ships releases; an unknown
# model just reports "up to date" (never a false alarm).
LATEST_FIRMWARE: Final[dict[str, str]] = {
    # GS110TP — kb.netgear.com/000060348
    "1.3.6.1.4.1.4526.100.4.19": "5.4.2.33",
    # GS516TP — last release for this end-of-life model
    "1.3.6.1.4.1.4526.100.4.29": "6.0.1.16",
}
