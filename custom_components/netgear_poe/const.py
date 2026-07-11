"""Constants for the Netgear PoE Switch integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "netgear_poe"

CONF_COMMUNITY: Final = "community"
CONF_ENABLE_TRAPS: Final = "enable_traps"

SCAN_INTERVAL_SECONDS: Final = 30

PLATFORMS: Final[list[str]] = ["binary_sensor", "button", "sensor", "switch"]
