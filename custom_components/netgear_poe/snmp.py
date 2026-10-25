"""Optional SNMP reader for Netgear switches.

The GS728TPv2 firmware only exposes read-only MIB-2 over SNMP, but that
includes IF-MIB ifOperStatus (per-port link up/down) and ifAlias (the port
names, same as LibreNMS reads). SNMP is treated as best-effort: the agent on
this firmware is known to hang occasionally, so failures degrade gracefully
instead of breaking the integration.

PoE state is read here too when the switch implements POWER-ETHERNET-MIB, so
the web UI is only needed to change something. Reads over the web UI log in,
hold one of a handful of management sessions, and on the GS728TP's final
firmware (6.0.1.30) the PoE status page hangs outright, which the coordinator
used to report as a stalled PoE controller while power was flowing normally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .api import PoeData, PoePort

_LOGGER = logging.getLogger(__name__)

OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
# POWER-ETHERNET-MIB pethPsePortAdminEnable, indexed <group>.<port>. It is a
# TruthValue, so 1 = enabled and 2 = disabled — 0 is rejected.
OID_PSE_PORT_ADMIN = "1.3.6.1.2.1.105.1.1.1.3"
PSE_GROUP = 1
PSE_ENABLED = 1
PSE_DISABLED = 2
# pethPsePortDetectionStatus, same codes the legacy web UI uses.
OID_PSE_PORT_DETECTION = "1.3.6.1.2.1.105.1.1.1.6"
PSE_DETECTION_STATUS = {
    1: "disabled",
    2: "searching",
    3: "delivering",
    4: "fault",
    5: "test",
    6: "otherfault",
}
# pethMainPseConsumptionPower, whole watts, indexed by PSE group.
OID_PSE_MAIN_CONSUMPTION = "1.3.6.1.2.1.105.1.3.1.1.4"
# The standard MIB has no per-port power. Vendor tables that do, in mW, both
# indexed <group>.<port>; tried in order and the first that answers is kept:
# Netgear FASTPATH agentPethOutputPower (S350 / GS324TP) and Marvell/RADLAN
# rlPethPsePortOutputPower (legacy xui firmware, e.g. GS728TP 6.0.x). Switches
# with neither (GS728TPPv3) report no per-port watts.
PORT_POWER_MW_OIDS = (
    "1.3.6.1.4.1.4526.11.15.1.1.1.2",
    "1.3.6.1.4.1.89.108.1.1.5",
)
# ifIndex values above this are LAGs/CPU interfaces, not physical ports
MAX_PHYSICAL_PORT = 64
# Columns are read with batched GETs of known rows rather than walks: some
# agents (GS728TP 6.0.x) answer each request in ~0.5 s and time out on bulk
# requests, so a 24-row walk takes ~16 s (when it finishes at all) while one GET
# of 24 rows takes ~3 s. Known rows are re-read every so often to pick up changes.
GET_BATCH_SIZE = 25
REWALK_EVERY_READS = 120
# GETBULK sizes to try, largest first (see _walk_rows).
WALK_MAX_REPETITIONS = (25, 5)
# After the per-port power tables time out, skip them for this many reads:
# probing a hung vendor table costs ~20 s, which would double every poll.
POWER_PROBE_BACKOFF_READS = 20


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
        # None until the first PoE read: then True if the switch implements
        # POWER-ETHERNET-MIB, False if SNMP answers without it (web reads only).
        self.poe_supported: bool | None = None
        # The per-port power table that answered: None = not probed yet,
        # "" = no table on this switch.
        self._power_oid: str | None = None
        self._power_skip = 0
        # Row suffixes per column from its last walk, and reads since then.
        self._rows: dict[str, list[tuple[int, ...]]] = {}
        self._reads: dict[str, int] = {}

    async def async_get_port_info(self) -> tuple[dict[int, bool], dict[int, str]]:
        """Return ({port: link_up}, {port: name}); empty dicts if SNMP is down."""
        try:
            oper = await self._read(OID_IF_OPER_STATUS)
            alias = await self._read(OID_IF_ALIAS)
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

    async def async_get_poe_data(self) -> PoeData | None:
        """Read PoE state over SNMP, or None to fall back to the web UI.

        Admin state and detection status are required; per-port watts are
        best-effort, since some agents hang on the vendor tables. Ports are
        keyed by front-panel number (the PSE port index, group 1).
        """
        if self.poe_supported is False:
            return None
        try:
            # PSE rows are <group>.<port> for front-panel ports, which IF-MIB
            # already lists; fetching those directly avoids walking the tables.
            ports = sorted(
                p
                for p in await self._read(OID_IF_OPER_STATUS)
                if p <= MAX_PHYSICAL_PORT
            )
            rows = [(PSE_GROUP, p) for p in ports]
            admin = await self._read(OID_PSE_PORT_ADMIN, rows)
            detection = await self._read(OID_PSE_PORT_DETECTION, rows)
            main = await self._read(OID_PSE_MAIN_CONSUMPTION, [(PSE_GROUP,)])
        except Exception as err:
            _LOGGER.debug("SNMP PoE read failed on %s: %s", self.host, err)
            return None
        if not admin:
            # The agent answered but has no PSE table: stop asking.
            if self.poe_supported is None:
                _LOGGER.info(
                    "%s has no POWER-ETHERNET-MIB; reading PoE over the web UI",
                    self.host,
                )
            self.poe_supported = False
            return None
        self.poe_supported = True
        power_mw = await self._port_power_mw([(PSE_GROUP, p) for p in sorted(admin)])
        data = PoeData()
        for port, value in admin.items():
            if port > MAX_PHYSICAL_PORT:
                continue
            mw = power_mw.get(port)
            data.ports[port] = PoePort(
                port=port,
                admin_enabled=int(value) == PSE_ENABLED,
                detection_status=PSE_DETECTION_STATUS.get(
                    int(detection.get(port, 0)), "unknown"
                ),
                power_watts=round(int(mw) / 1000, 1) if mw is not None else None,
            )
        if power_mw:
            # Same basis as the web reads, which sum the per-port figures.
            data.consumption_watts = round(
                sum(int(v) for p, v in power_mw.items() if p in data.ports) / 1000, 1
            )
        elif PSE_GROUP in main:
            data.consumption_watts = float(int(main[PSE_GROUP]))
        return data

    async def _port_power_mw(self, rows: list[tuple[int, ...]]) -> dict[int, Any]:
        """Per-port output power in mW from whichever vendor table exists."""
        if self._power_oid == "":
            return {}
        if self._power_skip:
            self._power_skip -= 1
            return {}
        candidates = (self._power_oid,) if self._power_oid else PORT_POWER_MW_OIDS
        failed = False
        for oid in candidates:
            try:
                values = await self._read(oid, rows)
            except Exception as err:
                # A hang here costs only the per-port watts, not the read.
                _LOGGER.debug("SNMP %s failed on %s: %s", oid, self.host, err)
                failed = True
                continue
            if values:
                self._power_oid = oid
                return values
        # Only conclude there is no table when every candidate answered empty;
        # after a timeout, try again a while later.
        if self._power_oid is None and not failed:
            self._power_oid = ""
        elif failed:
            self._power_skip = POWER_PROBE_BACKOFF_READS
        return {}

    async def _read(
        self, oid: str, candidates: list[tuple[int, ...]] | None = None
    ) -> dict[int, Any]:
        """Read a column, keyed like _walk.

        Without `candidates` the column is walked the first time, then its
        known rows are fetched with batched GETs; a failed GET (or age) falls
        back to a fresh walk. With `candidates` the column is never walked:
        those rows are fetched directly and whichever exist are kept. Walking
        is what some agents choke on (GS728TP 6.0.x times out on most walks of
        the PSE tables), while GETs of known rows answer reliably.
        """
        rows = self._rows.get(oid)
        reads = self._reads.get(oid, 0)
        if rows and reads < REWALK_EVERY_READS:
            try:
                values = await self._get_rows(oid, rows)
            except Exception as err:
                if candidates is not None:
                    raise
                _LOGGER.debug("SNMP GET of %s failed on %s: %s", oid, self.host, err)
            else:
                self._reads[oid] = reads + 1
                return {row[-1]: value for row, value in values.items()}
        if candidates is not None:
            found = await self._get_rows(oid, candidates, missing_ok=True)
        else:
            found = await self._walk_rows(oid)
        self._rows[oid] = list(found)
        self._reads[oid] = 0
        return {row[-1]: value for row, value in found.items()}

    async def _get_rows(
        self, oid: str, rows: list[tuple[int, ...]], missing_ok: bool = False
    ) -> dict[tuple[int, ...], Any]:
        """GET rows of a column in batches. Raises on any failure.

        A row the agent says does not exist raises too (so cached rows that
        vanished force a re-read), unless `missing_ok`, which just drops it.
        """
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            UdpTransportTarget,
            get_cmd,
        )
        from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

        engine = await self._get_engine()
        target = await UdpTransportTarget.create((self.host, 161), timeout=5, retries=1)
        result: dict[tuple[int, ...], Any] = {}
        for start in range(0, len(rows), GET_BATCH_SIZE):
            batch = rows[start : start + GET_BATCH_SIZE]
            err_indication, err_status, _, var_binds = await get_cmd(
                engine,
                CommunityData(self._community, mpModel=1),
                target,
                ContextData(),
                *(
                    ObjectType(ObjectIdentity(f"{oid}.{'.'.join(map(str, row))}"))
                    for row in batch
                ),
            )
            if err_indication:
                raise RuntimeError(str(err_indication))
            if err_status:
                raise RuntimeError(err_status.prettyPrint())
            for row, var_bind in zip(batch, var_binds, strict=True):
                if isinstance(
                    var_bind[1], (NoSuchInstance, NoSuchObject, EndOfMibView)
                ):
                    if missing_ok:
                        continue
                    raise RuntimeError(f"{oid} row {row} no longer exists")
                result[row] = var_bind[1]
        return result

    async def _get_engine(self) -> Any:
        if self._engine is None:
            loop = asyncio.get_running_loop()
            self._engine = await loop.run_in_executor(None, _build_engine)
        return self._engine

    async def _walk(self, oid: str) -> dict[int, Any]:
        """Walk a column, returning {last index component: value}."""
        return {
            suffix[-1]: value for suffix, value in (await self._walk_rows(oid)).items()
        }

    async def _walk_rows(self, oid: str) -> dict[tuple[int, ...], Any]:
        """Walk a table column, returning {row index suffix: value}.

        Callers key by the last component: for IF-MIB that is the ifIndex, for
        the PSE tables (<group>.<port>) the port, which assumes the single PSE
        group these switches have. Some agents time out on a large GETBULK but
        answer a small one, so a timeout retries with fewer repetitions.
        """
        last_error: Exception | None = None
        for max_repetitions in WALK_MAX_REPETITIONS:
            try:
                return await self._bulk_walk(oid, max_repetitions)
            except TimeoutError as err:
                last_error = err
                _LOGGER.debug(
                    "SNMP walk of %s timed out on %s at %d repetitions",
                    oid,
                    self.host,
                    max_repetitions,
                )
        assert last_error is not None
        raise last_error

    async def _bulk_walk(
        self, oid: str, max_repetitions: int
    ) -> dict[tuple[int, ...], Any]:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            UdpTransportTarget,
            bulk_walk_cmd,
        )

        engine = await self._get_engine()
        target = await UdpTransportTarget.create((self.host, 161), timeout=5, retries=1)
        base = tuple(int(x) for x in oid.split("."))
        result: dict[tuple[int, ...], Any] = {}
        async for err_indication, err_status, _, var_binds in bulk_walk_cmd(
            engine,
            CommunityData(self._community, mpModel=1),
            target,
            ContextData(),
            0,
            max_repetitions,
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if err_indication:
                message = str(err_indication)
                if "timeout" in message.lower():
                    raise TimeoutError(message)
                raise RuntimeError(message)
            if err_status:
                raise RuntimeError(err_status.prettyPrint())
            for var_bind in var_binds:
                var_oid = tuple(var_bind[0])
                if var_oid[: len(base)] != base:
                    continue
                result[var_oid[len(base) :]] = var_bind[1]
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
