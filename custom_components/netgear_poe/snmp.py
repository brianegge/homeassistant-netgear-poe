"""Optional SNMP reader for Netgear switches.

The GS728TPv2 firmware only exposes read-only MIB-2 over SNMP, but that
includes IF-MIB ifOperStatus (per-port link up/down) and ifAlias (the port
names, same as LibreNMS reads). SNMP is treated as best-effort: the agent on
this firmware is known to hang occasionally, so failures degrade gracefully
instead of breaking the integration.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
# ifIndex values above this are LAGs/CPU interfaces, not physical ports
MAX_PHYSICAL_PORT = 64


class SnmpLinkMonitor:
    """Read per-port link state and names via SNMP (pysnmp, v2c)."""

    def __init__(self, host: str, community: str) -> None:
        self.host = host
        self._community = community
        self._engine: Any | None = None
        self._was_available = True

    async def async_get_port_info(self) -> tuple[dict[int, bool], dict[int, str]]:
        """Return ({port: link_up}, {port: name}); empty dicts if SNMP is down."""
        try:
            oper = await self._walk(OID_IF_OPER_STATUS)
            alias = await self._walk(OID_IF_ALIAS)
        except Exception as err:  # noqa: BLE001 - degrade on any SNMP failure
            if self._was_available:
                _LOGGER.warning("SNMP unavailable on %s: %s", self.host, err)
                self._was_available = False
            return {}, {}
        if not self._was_available:
            _LOGGER.info("SNMP available again on %s", self.host)
            self._was_available = True

        states = {
            port: int(value) == 1  # ifOperStatus: 1=up, 2=down
            for port, value in oper.items()
            if port <= MAX_PHYSICAL_PORT
        }
        names = {
            port: str(value).strip()
            for port, value in alias.items()
            if port <= MAX_PHYSICAL_PORT and str(value).strip()
        }
        return states, names

    async def _walk(self, oid: str) -> dict[int, Any]:
        """Walk an IF-MIB column, returning {ifIndex: value}."""
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            bulk_walk_cmd,
        )

        if self._engine is None:
            self._engine = SnmpEngine()
        target = await UdpTransportTarget.create((self.host, 161), timeout=5, retries=1)
        base = tuple(int(x) for x in oid.split("."))
        result: dict[int, Any] = {}
        async for err_indication, err_status, _, var_binds in bulk_walk_cmd(
            self._engine,
            CommunityData(self._community, mpModel=1),
            target,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if err_indication:
                raise RuntimeError(str(err_indication))
            if err_status:
                raise RuntimeError(err_status.prettyPrint())
            for var_bind in var_binds:
                var_oid = tuple(var_bind[0])
                if var_oid[: len(base)] != base:
                    continue
                result[var_oid[-1]] = var_bind[1]
        return result

    async def async_close(self) -> None:
        """Shut down the SNMP engine transport."""
        if self._engine is not None:
            dispatcher = self._engine.transport_dispatcher
            if dispatcher is not None:
                dispatcher.close_dispatcher()
            self._engine = None
