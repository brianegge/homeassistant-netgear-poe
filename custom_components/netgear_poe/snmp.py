"""Optional SNMP reader for Netgear switches.

The GS728TPv2 firmware only exposes read-only MIB-2 over SNMP, but that
includes IF-MIB ifOperStatus (per-port link up/down) and ifAlias (the port
names, same as LibreNMS reads). SNMP is treated as best-effort: the agent on
this firmware is known to hang occasionally, so failures degrade gracefully
instead of breaking the integration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
# POWER-ETHERNET-MIB pethPsePortAdminEnable, indexed <group>.<port>. It is a
# TruthValue, so 1 = enabled and 2 = disabled — 0 is rejected.
OID_PSE_PORT_ADMIN = "1.3.6.1.2.1.105.1.1.1.3"
PSE_GROUP = 1
PSE_ENABLED = 1
PSE_DISABLED = 2
# ifIndex values above this are LAGs/CPU interfaces, not physical ports
MAX_PHYSICAL_PORT = 64


def _build_engine() -> Any:
    """Build the SNMP engine with its MIB modules already compiled.

    pysnmp compiles its MIB modules lazily by reading them off disk — blocking
    filesystem I/O that Home Assistant flags when it runs on the event loop.
    Constructing the engine and forcing `load_modules()` here means this
    executor-thread call absorbs the bulk of that work, off-loop. (pysnmp still
    does a small one-time source scan inside its first command that can't be
    pre-warmed without issuing a full SNMP request, which is unsafe to do
    during setup — so a brief first-poll scan may remain.)
    """
    from pysnmp.hlapi.v3arch.asyncio import SnmpEngine

    engine = SnmpEngine()
    engine.get_mib_builder().load_modules()
    return engine


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
        except Exception as err:
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
            UdpTransportTarget,
            bulk_walk_cmd,
        )

        if self._engine is None:
            loop = asyncio.get_running_loop()
            self._engine = await loop.run_in_executor(None, _build_engine)
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

    async def async_set_poe_enabled(self, port: int, enabled: bool) -> None:
        """Set pethPsePortAdminEnable for a port. Raises on any failure.

        The web UI is the primary write path; this exists because some
        firmware refuses the state-changing POST while still answering SNMP
        (the S350/cheetah generation returns a bare 403). Requires the
        configured community to have write access — a read-only community
        answers noAccess/notWritable, which surfaces as an exception here.
        """
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            Integer,
            ObjectIdentity,
            ObjectType,
            UdpTransportTarget,
            set_cmd,
        )

        if self._engine is None:
            loop = asyncio.get_running_loop()
            self._engine = await loop.run_in_executor(None, _build_engine)
        oid = f"{OID_PSE_PORT_ADMIN}.{PSE_GROUP}.{port}"
        value = PSE_ENABLED if enabled else PSE_DISABLED
        target = await UdpTransportTarget.create((self.host, 161), timeout=5, retries=1)
        err_indication, err_status, _, var_binds = await set_cmd(
            self._engine,
            CommunityData(self._community, mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(oid), Integer(value)),
        )
        if err_indication:
            raise RuntimeError(f"SNMP set {oid} failed: {err_indication}")
        if err_status:
            raise RuntimeError(f"SNMP set {oid} rejected: {err_status.prettyPrint()}")
        # A switch can acknowledge the set and ignore it; confirm the value
        # actually took rather than reporting a success we did not verify.
        got = next((int(v[1]) for v in var_binds), None)
        if got != value:
            raise RuntimeError(
                f"SNMP set {oid} did not stick (wanted {value}, got {got})"
            )
        _LOGGER.debug("SNMP set %s = %s on %s", oid, value, self.host)

    async def async_power_cycle(self, port: int, off_seconds: float) -> None:
        """Drop and restore PoE on a port over SNMP.

        Power is restored even if the wait is cancelled, and the restore is
        retried, so a transient failure cannot leave the port dark.
        """
        await self.async_set_poe_enabled(port, False)
        try:
            await asyncio.sleep(off_seconds)
        finally:
            for attempt in range(3):
                try:
                    await self.async_set_poe_enabled(port, True)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)

    async def async_close(self) -> None:
        """Shut down the SNMP engine transport."""
        if self._engine is not None:
            dispatcher = self._engine.transport_dispatcher
            if dispatcher is not None:
                dispatcher.close_dispatcher()
            self._engine = None
