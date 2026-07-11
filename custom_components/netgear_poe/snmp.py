"""Optional SNMP link-state reader for Netgear switches.

The GS728TPv2 firmware only exposes read-only MIB-2 over SNMP, but that
includes IF-MIB ifOperStatus — enough for per-port link up/down sensors.
SNMP is treated as best-effort: the agent on this firmware is known to hang
occasionally, so failures degrade the link sensors instead of breaking the
integration.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
# ifIndex values above this are LAGs/CPU interfaces, not physical ports
MAX_PHYSICAL_PORT = 64


class SnmpLinkMonitor:
    """Read per-port link state via SNMP (pysnmp, v2c)."""

    def __init__(self, host: str, community: str) -> None:
        self.host = host
        self._community = community
        self._engine: Any | None = None
        self._was_available = True

    async def async_get_link_states(self) -> dict[int, bool]:
        """Return {port: link_up}. Empty dict when SNMP is unavailable."""
        try:
            states = await self._walk_oper_status()
        except Exception as err:  # noqa: BLE001 - degrade on any SNMP failure
            if self._was_available:
                _LOGGER.warning(
                    "SNMP link state unavailable on %s: %s", self.host, err
                )
                self._was_available = False
            return {}
        if not self._was_available:
            _LOGGER.info("SNMP link state available again on %s", self.host)
            self._was_available = True
        return states

    async def _walk_oper_status(self) -> dict[int, bool]:
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
        base = tuple(int(x) for x in OID_IF_OPER_STATUS.split("."))
        states: dict[int, bool] = {}
        async for err_indication, err_status, _, var_binds in bulk_walk_cmd(
            self._engine,
            CommunityData(self._community, mpModel=1),
            target,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(OID_IF_OPER_STATUS)),
            lexicographicMode=False,
        ):
            if err_indication:
                raise RuntimeError(str(err_indication))
            if err_status:
                raise RuntimeError(err_status.prettyPrint())
            for var_bind in var_binds:
                oid = tuple(var_bind[0])
                if oid[: len(base)] != base:
                    continue
                if_index = oid[-1]
                if if_index <= MAX_PHYSICAL_PORT:
                    # ifOperStatus: 1=up, 2=down, ...
                    states[if_index] = int(var_bind[1]) == 1
        return states

    async def async_close(self) -> None:
        """Shut down the SNMP engine transport."""
        if self._engine is not None:
            dispatcher = self._engine.transport_dispatcher
            if dispatcher is not None:
                dispatcher.close_dispatcher()
            self._engine = None
