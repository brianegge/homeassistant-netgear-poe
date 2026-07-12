"""Constants for the Netgear PoE Switch integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "netgear_poe"

CONF_COMMUNITY: Final = "community"
CONF_ENABLE_TRAPS: Final = "enable_traps"

SCAN_INTERVAL_SECONDS: Final = 30

# NSDP discovery: switches answer probabilistically, so each scan runs a while.
DISCOVERY_SCAN_SECONDS: Final = 45
DISCOVERY_INTERVAL_SECONDS: Final = 600

PLATFORMS: Final[list[str]] = ["binary_sensor", "button", "sensor", "switch"]
