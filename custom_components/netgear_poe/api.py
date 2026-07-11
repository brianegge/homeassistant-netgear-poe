"""SNMP client for Netgear PoE switches (POWER-ETHERNET-MIB)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .const import (
    DETECTION_STATUS,
    OID_IF_ALIAS,
    OID_MAIN_CONSUMPTION_POWER,
    OID_PORT_ADMIN_ENABLE,
    OID_PORT_DETECTION_STATUS,
    OID_SYS_DESCR,
    OID_SYS_NAME,
)

_LOGGER = logging.getLogger(__name__)


class SnmpError(Exception):
    """SNMP request failed."""


@dataclass
class PoePort:
    """State of a single PoE port."""

    port: int
    admin_enabled: bool
    detection_status: str = "searching"
    alias: str = ""


@dataclass
class PoeData:
    """State of the switch."""

    ports: dict[int, PoePort] = field(default_factory=dict)
    consumption_watts: int | None = None
    sys_name: str = ""
    sys_descr: str = ""


class NetgearPoeApi:
    """Thin async SNMP wrapper for PoE port control.

    All pysnmp imports are deferred so the module can be imported (and
    mocked in tests) without pysnmp installed.
    """

    def __init__(
        self,
        host: str,
        community: str,
        write_community: str,
        port: int = 161,
    ) -> None:
        self.host = host
        self._port = port
        self._community = community
        self._write_community = write_community or community
        self._engine: Any | None = None

    async def _get_engine(self) -> Any:
        if self._engine is None:
            from pysnmp.hlapi.v3arch.asyncio import SnmpEngine

            self._engine = SnmpEngine()
        return self._engine

    async def _snmp_args(self, write: bool = False) -> tuple[Any, Any, Any, Any]:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            UdpTransportTarget,
        )

        engine = await self._get_engine()
        community = self._write_community if write else self._community
        auth = CommunityData(community, mpModel=1)
        target = await UdpTransportTarget.create(
            (self.host, self._port), timeout=5, retries=1
        )
        return engine, auth, target, ContextData()

    async def _walk(self, base_oid: str) -> dict[tuple[int, ...], Any]:
        """Walk a subtree, returning {oid suffix: value}."""
        from pysnmp.hlapi.v3arch.asyncio import (
            ObjectIdentity,
            ObjectType,
            bulk_walk_cmd,
        )

        args = await self._snmp_args()
        base = tuple(int(x) for x in base_oid.split("."))
        results: dict[tuple[int, ...], Any] = {}
        async for err_indication, err_status, err_index, var_binds in bulk_walk_cmd(
            *args,
            0,
            25,
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if err_indication:
                raise SnmpError(str(err_indication))
            if err_status:
                raise SnmpError(err_status.prettyPrint())
            for var_bind in var_binds:
                oid = tuple(var_bind[0])
                if oid[: len(base)] != base:
                    continue
                results[oid[len(base) :]] = var_bind[1]
        return results

    async def _get(self, oid: str) -> Any:
        from pysnmp.hlapi.v3arch.asyncio import ObjectIdentity, ObjectType, get_cmd

        args = await self._snmp_args()
        err_indication, err_status, err_index, var_binds = await get_cmd(
            *args, ObjectType(ObjectIdentity(oid))
        )
        if err_indication:
            raise SnmpError(str(err_indication))
        if err_status:
            raise SnmpError(err_status.prettyPrint())
        return var_binds[0][1]

    async def async_set_port_enabled(self, port: int, enabled: bool) -> None:
        """Enable or disable PoE on a port (pethPsePortAdminEnable)."""
        from pysnmp.hlapi.v3arch.asyncio import ObjectIdentity, ObjectType, set_cmd
        from pysnmp.proto.rfc1902 import Integer

        args = await self._snmp_args(write=True)
        oid = f"{OID_PORT_ADMIN_ENABLE}.1.{port}"
        err_indication, err_status, err_index, var_binds = await set_cmd(
            *args, ObjectType(ObjectIdentity(oid), Integer(1 if enabled else 2))
        )
        if err_indication:
            raise SnmpError(str(err_indication))
        if err_status:
            raise SnmpError(
                f"SNMP set failed: {err_status.prettyPrint()}"
                " (check the write community)"
            )

    async def async_get_data(self) -> PoeData:
        """Fetch PoE state for all ports."""
        admin = await self._walk(OID_PORT_ADMIN_ENABLE)
        if not admin:
            raise SnmpError("No PoE ports found (empty pethPsePortTable)")
        status = await self._walk(OID_PORT_DETECTION_STATUS)
        aliases = await self._walk(OID_IF_ALIAS)

        data = PoeData()
        for suffix, value in admin.items():
            # pethPsePortTable is indexed by (group, port)
            port = suffix[-1]
            port_status = status.get(suffix)
            data.ports[port] = PoePort(
                port=port,
                admin_enabled=int(value) == 1,
                detection_status=DETECTION_STATUS.get(
                    int(port_status) if port_status is not None else 2, "searching"
                ),
                alias=str(aliases.get((port,), "")).strip(),
            )

        try:
            power = await self._walk(OID_MAIN_CONSUMPTION_POWER)
            if power:
                data.consumption_watts = sum(int(v) for v in power.values())
        except SnmpError:
            _LOGGER.debug("Switch does not report PoE consumption")

        return data

    async def async_get_info(self) -> tuple[str, str]:
        """Return (sysName, sysDescr) for device info."""
        sys_name = str(await self._get(OID_SYS_NAME))
        sys_descr = str(await self._get(OID_SYS_DESCR))
        return sys_name, sys_descr

    async def async_close(self) -> None:
        """Shut down the SNMP engine transport."""
        if self._engine is not None:
            dispatcher = self._engine.transport_dispatcher
            if dispatcher is not None:
                dispatcher.close_dispatcher()
            self._engine = None
