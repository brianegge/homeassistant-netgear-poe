"""Constants for the Netgear PoE Switch integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "netgear_poe"

CONF_COMMUNITY: Final = "community"
CONF_WRITE_COMMUNITY: Final = "write_community"

DEFAULT_COMMUNITY: Final = "public"
DEFAULT_SNMP_PORT: Final = 161

SCAN_INTERVAL_SECONDS: Final = 30
POWER_CYCLE_DELAY_SECONDS: Final = 5

# POWER-ETHERNET-MIB (RFC 3621)
OID_PORT_ADMIN_ENABLE: Final = "1.3.6.1.2.1.105.1.1.1.3"
OID_PORT_DETECTION_STATUS: Final = "1.3.6.1.2.1.105.1.1.1.6"
OID_MAIN_CONSUMPTION_POWER: Final = "1.3.6.1.2.1.105.1.3.1.1.4"
# IF-MIB
OID_IF_ALIAS: Final = "1.3.6.1.2.1.31.1.1.1.18"
OID_SYS_DESCR: Final = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME: Final = "1.3.6.1.2.1.1.5.0"

DETECTION_STATUS: Final[dict[int, str]] = {
    1: "disabled",
    2: "searching",
    3: "delivering_power",
    4: "fault",
    5: "test",
    6: "other_fault",
}

PLATFORMS: Final[list[str]] = ["button", "sensor", "switch"]
