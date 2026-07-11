"""Constants for the Netgear PoE Switch integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "netgear_poe"

SCAN_INTERVAL_SECONDS: Final = 30

PLATFORMS: Final[list[str]] = ["button", "sensor", "switch"]
