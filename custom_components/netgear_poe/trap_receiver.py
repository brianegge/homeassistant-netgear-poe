"""SNMP trap receiver for instant Netgear switch link/PoE events.

Home Assistant has no built-in trap receiver, so this opens a pysnmp
NotificationReceiver on UDP:162 and decodes standard linkUp/linkDown traps
(and PoE on/off notifications) into per-port link updates. Traps are
best-effort (UDP), so the SNMP poll still runs as an authoritative backstop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_TRAP_PORT = 162

# RFC 1907 / IF-MIB notification OIDs
OID_SNMP_TRAP = "1.3.6.1.6.3.1.1.4.1.0"
OID_LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
OID_LINK_UP = "1.3.6.1.6.3.1.1.5.4"
OID_IF_INDEX = "1.3.6.1.2.1.2.2.1.1"
# POWER-ETHERNET-MIB pethPsePortOnOffNotification
OID_PETH_PSE_PORT_NOTIFY = "1.3.6.1.2.1.105.0.1"

# ifIndex values above this are LAGs/CPU interfaces, not physical ports
MAX_PHYSICAL_PORT = 64


class SnmpTrapReceiver:
    """Listen for SNMP traps and report per-port link changes."""

    def __init__(
        self,
        community: str,
        on_link_change: Callable[[int, bool], None],
        on_poe_event: Callable[[int], None] | None = None,
        bind_host: str = "0.0.0.0",  # noqa: S104 - traps arrive on all ifaces
        bind_port: int = DEFAULT_TRAP_PORT,
        source_host: str | None = None,
    ) -> None:
        self._community = community
        self._on_link_change = on_link_change
        self._on_poe_event = on_poe_event
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._source_host = source_host
        self._engine: Any | None = None

    async def async_start(self) -> None:
        """Open the UDP listener. Must run inside the event loop."""
        from pysnmp.carrier.asyncio.dgram import udp
        from pysnmp.entity import config, engine
        from pysnmp.entity.rfc3413 import ntfrcv

        rx_engine = engine.SnmpEngine()
        config.add_transport(
            rx_engine,
            udp.DOMAIN_NAME,
            udp.UdpTransport().open_server_mode((self._bind_host, self._bind_port)),
        )
        config.add_v1_system(rx_engine, "netgear-poe", self._community)
        ntfrcv.NotificationReceiver(rx_engine, self._on_trap)
        self._engine = rx_engine
        _LOGGER.debug(
            "SNMP trap receiver listening on %s:%d", self._bind_host, self._bind_port
        )

    def _on_trap(
        self,
        snmp_engine: Any,
        state_reference: Any,
        context_engine_id: Any,
        context_name: Any,
        var_binds: Any,
        cb_ctx: Any,
    ) -> None:
        """Handle one decoded trap (called in the event loop)."""
        try:
            binds = {str(oid): value for oid, value in var_binds}
        except Exception:  # noqa: BLE001 - never let a bad trap kill the loop
            _LOGGER.debug("Failed to parse trap var_binds", exc_info=True)
            return

        trap_oid = str(binds.get(OID_SNMP_TRAP, ""))
        if_index = _extract_if_index(binds)
        _LOGGER.debug("Trap received: %s (ifIndex=%s)", trap_oid, if_index)

        if trap_oid in (OID_LINK_UP, OID_LINK_DOWN):
            if if_index is None or if_index > MAX_PHYSICAL_PORT:
                return
            up = trap_oid == OID_LINK_UP
            _LOGGER.debug("Trap: port %d link %s", if_index, "up" if up else "down")
            self._on_link_change(if_index, up)
        elif trap_oid == OID_PETH_PSE_PORT_NOTIFY and self._on_poe_event is not None:
            if if_index is not None and if_index <= MAX_PHYSICAL_PORT:
                self._on_poe_event(if_index)

    async def async_stop(self) -> None:
        """Close the UDP listener."""
        if self._engine is not None:
            dispatcher = self._engine.transport_dispatcher
            if dispatcher is not None:
                dispatcher.close_dispatcher()
            self._engine = None


def _extract_if_index(binds: dict[str, Any]) -> int | None:
    """Pull the ifIndex value out of a trap's var_binds."""
    for oid, value in binds.items():
        if oid == OID_IF_INDEX or oid.startswith(OID_IF_INDEX + "."):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None
