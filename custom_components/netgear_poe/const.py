"""Constants for the Netgear PoE Switch integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

DOMAIN: Final = "netgear_poe"

CONF_COMMUNITY: Final = "community"
CONF_ENABLE_TRAPS: Final = "enable_traps"  # legacy bool; superseded by trap_mode
CONF_TRAP_MODE: Final = "trap_mode"
CONF_TRAP_BRIDGE_HOST: Final = "trap_bridge_host"
CONF_TRAP_TOPIC: Final = "trap_topic"

TRAP_MODE_LOCAL: Final = "local"
TRAP_MODE_MQTT: Final = "mqtt"
TRAP_MODE_DISABLED: Final = "disabled"
TRAP_MODES: Final[list[str]] = [TRAP_MODE_LOCAL, TRAP_MODE_MQTT, TRAP_MODE_DISABLED]

# Base topic of the snmptrap2mqtt bridge feeding TRAP_MODE_MQTT.
DEFAULT_TRAP_TOPIC: Final = "snmptrap2mqtt"

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


@dataclass(frozen=True)
class FirmwareRelease:
    """A firmware release Netgear ships for a switch model."""

    version: str
    # Direct download; Netgear zips the flashable .stk with its release notes.
    url: str | None = None
    # Human-readable release notes (KB article).
    notes_url: str | None = None


# Latest firmware per model, maintained by hand from Netgear's download pages.
# Keyed by SNMP sysObjectID (model-exact) with the clean model name as a
# fallback key. When a switch matches, HA surfaces an "update available" if it
# is running something older. Bump these as Netgear ships releases; an unknown
# model just reports "up to date" (never a false alarm).
#
# Take the sysObjectID from the KB article's *exact* model list, not from a
# family name: entries here choose the image the update entity downloads and
# writes to a live switch, so a wrong key flashes the wrong hardware. Netgear
# ships GS110TP, GS110TPv2 and GS110TPv3 as different products under one
# family, and the v3 is different silicon that answers the JSON CGI API.
LATEST_FIRMWARE: Final[dict[str, FirmwareRelease]] = {
    # GS108Tv2 — still current, unlike the GS110TP (v1) it used to share a
    # release with; 5.4.2.36 names GS108Tv2 and GS110TPv2 only.
    "1.3.6.1.4.1.4526.100.4.18": FirmwareRelease(
        version="5.4.2.36",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS110TP/"
            "GS108Tv2_GS110TPv2_V5.4.2.36.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000064041/"
            "GS110TPv2-GS108Tv2-Firmware-Version-5-4-2-36"
        ),
    ),
    # GS110TP (v1) — 5.4.2.35 is the last release for this hardware. Do NOT
    # bump to 5.4.2.36: that article covers GS108Tv2 and GS110TP*v2*, and a
    # v1 reports "GS110TP" with this sysObjectID. Netgear only publishes
    # GS108Tv2_GS110TPv2_V5.4.2.36.zip — there is no v1 build of it.
    "1.3.6.1.4.1.4526.100.4.19": FirmwareRelease(
        version="5.4.2.35",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS110TP/"
            "GS108Tv2_GS110TP_V5.4.2.35.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000062837/"
            "GS110TP-GS108Tv2-Firmware-Version-5-4-2-35"
        ),
    ),
    # GS728TP (v1) — xui firmware, same 6.0.1.x line as the GS516TP; one zip
    # covers GS728TP/GS728TPP/GS752TP.
    "1.3.6.1.4.1.4526.100.4.25": FirmwareRelease(
        version="6.0.1.30",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS728TP/"
            "GS728TP_GS728TPP_GS752TP_V6.0.1.30.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000062486/"
            "GS728TP-GS728TPP-GS752TP-Firmware-Version-6-0-1-30"
        ),
    ),
    # GS516TP
    "1.3.6.1.4.1.4526.100.4.29": FirmwareRelease(
        version="6.0.1.30",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS516TP/GS516TP_V6.0.1.30.zip"
        ),
        notes_url="https://kb.netgear.com/000062485/GS516TP-Firmware-Version-6-0-1-30",
    ),
    # GS310TP on early 1.0.0.x firmware reports this sysObjectID (a switch on
    # 1.0.0.9 answers ...4.48 where its 1.0.1.x+ siblings answer ...4.53); the
    # same GS308T/GS310TP release applies.
    "1.3.6.1.4.1.4526.100.4.48": FirmwareRelease(
        version="1.0.5.12",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS308T/"
            "GS308T_GS310TP_V1.0.5.12.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000065851/"
            "GS308T-GS310TP-Firmware-Version-1-0-5-12"
        ),
    ),
    # GS308T / GS310TP (S350 hardware answering the JSON CGI API, which has no
    # install path — this entry only surfaces "update available" with notes).
    "1.3.6.1.4.1.4526.100.4.53": FirmwareRelease(
        version="1.0.5.12",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS308T/"
            "GS308T_GS310TP_V1.0.5.12.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000065851/"
            "GS308T-GS310TP-Firmware-Version-1-0-5-12"
        ),
    ),
    # GS324T / GS324TP (S350). Netgear ships one image for both, from the
    # GS324T directory.
    "1.3.6.1.4.1.4526.100.4.55": FirmwareRelease(
        version="1.0.0.44",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS324T/"
            "GS324Tv1_GS324TPv1_V1.0.0.44.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000066439/GS324T-GS324TP-Firmware-Version-1-0-0-44"
        ),
    ),
    # GS108Tv3 / GS110TPv3 / GS110TPP — one zip for the family.
    "1.3.6.1.4.1.4526.100.4.58": FirmwareRelease(
        version="7.1.1.13",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS108Tv3/"
            "GS108Tv3_GS110TPv3_GS110TPPv1_V7.1.1.13.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000070462/"
            "GS108Tv3-GS110TPv3-GS110TPP-Firmware-Version-7-1-1-13"
        ),
    ),
    # GS728TPv3 / GS728TPPv3 / GS752TPv3 / GS752TPPv3 — one zip for the family.
    "1.3.6.1.4.1.4526.100.4.69": FirmwareRelease(
        version="6.2.1.40",
        url=(
            "https://www.downloads.netgear.com/files/GDC/GS728TPPv3/"
            "GS728TPv3_GS728TPPv3_GS752TPv3_GS752TPPv3_V6.2.1.40.zip"
        ),
        notes_url=(
            "https://kb.netgear.com/000070655/"
            "GS728TPv3-GS728TPPv3-GS752TPv3-GS752TPPv3-Firmware-Version-6-2-1-40"
        ),
    ),
}
