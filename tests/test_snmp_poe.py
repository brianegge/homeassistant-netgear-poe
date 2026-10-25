"""Tests for reading PoE state over SNMP instead of the web UI."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearError, PoeData, PoePort
from custom_components.netgear_poe.snmp import (
    OID_IF_ALIAS,
    OID_IF_OPER_STATUS,
    OID_PSE_MAIN_CONSUMPTION,
    OID_PSE_PORT_ADMIN,
    OID_PSE_PORT_DETECTION,
    PORT_POWER_MW_OIDS,
    POWER_PROBE_BACKOFF_READS,
    REWALK_EVERY_READS,
    SnmpLinkMonitor,
)

from .conftest import setup_integration

FASTPATH_POWER, RADLAN_POWER = PORT_POWER_MW_OIDS


def snmp_poe_data() -> PoeData:
    """PoE data as the SNMP reader returns it (no aliases yet)."""
    return PoeData(
        ports={
            1: PoePort(
                port=1,
                admin_enabled=True,
                detection_status="delivering",
                power_watts=4.5,
            ),
            2: PoePort(
                port=2,
                admin_enabled=False,
                detection_status="disabled",
                power_watts=0.0,
            ),
        },
        consumption_watts=4.5,
    )


# ---------------------------------------------------------------- coordinator


async def test_snmp_poe_read_skips_the_web_ui(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """When SNMP answers, routine polls never touch the web UI."""
    mock_link_monitor.async_get_poe_data.return_value = snmp_poe_data()
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator

    mock_api.async_get_data.assert_not_called()
    assert coordinator.data.ports[1].power_watts == 4.5
    assert coordinator.data.ports[1].alias == "driveway cam"  # from ifAlias
    assert coordinator.data.link == {1: True, 2: False}
    assert coordinator.data.consumption_watts == 4.5


async def test_snmp_read_clears_stall_while_web_poe_hangs(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A hung web PoE page is not a stalled controller if SNMP reads it fine."""
    mock_api.async_get_data.side_effect = NetgearError("timeout")
    mock_link_monitor.async_get_poe_data.return_value = snmp_poe_data()
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator

    for _ in range(3):
        await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.poe_stalled is False


async def test_falls_back_to_web_when_snmp_poe_unavailable(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No SNMP PoE (hung agent, no PSE MIB) means the web read runs as before."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator

    mock_api.async_get_data.assert_called()
    assert coordinator.data.ports[1].power_watts == 6.5  # web figure


async def test_first_poll_without_snmp_names_uses_web(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With no names yet, the first poll reads the web UI so ports get named."""
    mock_link_monitor.async_get_poe_data.return_value = snmp_poe_data()
    mock_link_monitor.async_get_port_info.return_value = ({}, {})
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator

    mock_api.async_get_data.assert_called_once()
    assert coordinator.data.ports[1].alias == "driveway cam"  # web name


async def test_snmp_poll_keeps_last_names_when_ifalias_missing(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_link_monitor: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A poll whose ifAlias walk comes back empty must not blank port names."""
    mock_link_monitor.async_get_poe_data.return_value = snmp_poe_data()
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator

    mock_link_monitor.async_get_poe_data.return_value = snmp_poe_data()
    mock_link_monitor.async_get_port_info.return_value = ({1: True}, {})
    await coordinator.async_refresh()
    assert coordinator.data.ports[1].alias == "driveway cam"
    mock_api.async_get_data.assert_not_called()


# ---------------------------------------------------------------- SNMP reader


def monitor_with(walks: dict[str, Any]) -> SnmpLinkMonitor:
    """A monitor whose _read returns canned columns (or raises)."""
    monitor = SnmpLinkMonitor("192.0.2.1", "public")
    calls: list[str] = []

    async def fake_read(
        oid: str, candidates: list[tuple[int, ...]] | None = None
    ) -> dict[int, Any]:
        calls.append(oid)
        value = walks.get(oid, {})
        if isinstance(value, Exception):
            raise value
        return value

    monitor._read = fake_read  # type: ignore[method-assign]
    monitor.calls = calls  # type: ignore[attr-defined]
    return monitor


PSE = {
    OID_PSE_PORT_ADMIN: {1: 1, 2: 2, 3: 1},
    OID_PSE_PORT_DETECTION: {1: 3, 2: 1, 3: 2},
    OID_PSE_MAIN_CONSUMPTION: {1: 27},
}


async def test_reader_builds_ports_with_vendor_power() -> None:
    """Admin/detection from the standard MIB, watts from the first vendor table."""
    monitor = monitor_with({**PSE, FASTPATH_POWER: {1: 4500, 2: 0, 3: 0}})
    data = await monitor.async_get_poe_data()

    assert data is not None
    assert data.ports[1].admin_enabled is True
    assert data.ports[1].detection_status == "delivering"
    assert data.ports[1].power_watts == 4.5
    assert data.ports[2].admin_enabled is False
    assert data.ports[2].detection_status == "disabled"
    assert data.ports[3].detection_status == "searching"
    assert data.consumption_watts == 4.5  # summed per-port, like the web reads
    assert monitor.poe_supported is True


async def test_reader_tries_next_power_table_and_remembers_it() -> None:
    """The legacy RADLAN table is used when FASTPATH has nothing, then cached."""
    monitor = monitor_with({**PSE, RADLAN_POWER: {1: 3500}})
    data = await monitor.async_get_poe_data()
    assert data is not None and data.ports[1].power_watts == 3.5

    monitor.calls.clear()
    await monitor.async_get_poe_data()
    assert FASTPATH_POWER not in monitor.calls  # not re-probed


async def test_reader_without_power_table_uses_main_consumption() -> None:
    """No vendor table (e.g. GS728TPPv3): no per-port watts, total from the MIB."""
    monitor = monitor_with(PSE)
    data = await monitor.async_get_poe_data()

    assert data is not None
    assert data.ports[1].power_watts is None
    assert data.consumption_watts == 27.0

    monitor.calls.clear()
    await monitor.async_get_poe_data()
    assert not set(PORT_POWER_MW_OIDS) & set(monitor.calls)  # gave up for good


async def test_reader_backs_off_power_table_after_a_timeout() -> None:
    """A hung vendor table costs that poll's watts, then is retried later."""
    monitor = monitor_with(
        {
            **PSE,
            FASTPATH_POWER: RuntimeError("timeout"),
            RADLAN_POWER: RuntimeError("timeout"),
        }
    )
    data = await monitor.async_get_poe_data()
    assert data is not None and data.ports[1].power_watts is None
    assert data.consumption_watts == 27.0  # falls back to the MIB total

    for _ in range(POWER_PROBE_BACKOFF_READS):
        monitor.calls.clear()
        await monitor.async_get_poe_data()
        assert FASTPATH_POWER not in monitor.calls  # backing off

    monitor.calls.clear()
    await monitor.async_get_poe_data()
    assert FASTPATH_POWER in monitor.calls  # tried again, not given up


@pytest.mark.parametrize("failure", [RuntimeError("No SNMP response received")])
async def test_reader_returns_none_when_agent_fails(failure: Exception) -> None:
    """A failing agent falls back to the web read, without ruling SNMP out."""
    monitor = monitor_with({OID_PSE_PORT_ADMIN: failure})
    assert await monitor.async_get_poe_data() is None
    assert monitor.poe_supported is None


async def test_reader_stops_asking_without_pse_mib() -> None:
    """An agent with no PSE table is asked once, then left to the web reads."""
    monitor = monitor_with({OID_IF_OPER_STATUS: {1: 1}, OID_IF_ALIAS: {}})
    assert await monitor.async_get_poe_data() is None
    assert monitor.poe_supported is False

    monitor.calls.clear()
    assert await monitor.async_get_poe_data() is None
    assert monitor.calls == []


# ------------------------------------------------------ walk once, then GET


def caching_monitor() -> tuple[SnmpLinkMonitor, list[str]]:
    """A monitor with _walk_rows/_get stubbed, recording which one ran."""
    monitor = SnmpLinkMonitor("192.0.2.1", "public")
    calls: list[str] = []
    rows = {(1, 1): 1, (1, 2): 2}

    async def walk_rows(oid: str) -> dict[tuple[int, ...], Any]:
        calls.append("walk")
        return dict(rows)

    async def get_rows(
        oid: str, wanted: list[tuple[int, ...]], missing_ok: bool = False
    ) -> dict[tuple[int, ...], Any]:
        calls.append("get")
        if not missing_ok:
            assert wanted == list(rows)
        return {row: rows[row] for row in wanted if row in rows}

    monitor._walk_rows = walk_rows  # type: ignore[method-assign]
    monitor._get_rows = get_rows  # type: ignore[method-assign]
    return monitor, calls


async def test_read_walks_once_then_gets_known_rows() -> None:
    """The first read walks; later reads GET the rows the walk found."""
    monitor, calls = caching_monitor()
    assert await monitor._read(OID_PSE_PORT_ADMIN) == {1: 1, 2: 2}
    assert await monitor._read(OID_PSE_PORT_ADMIN) == {1: 1, 2: 2}
    assert calls == ["walk", "get"]


async def test_read_rewalks_after_a_failed_get_and_periodically() -> None:
    """A failed GET (e.g. a vanished row) re-walks; so does age."""
    monitor, calls = caching_monitor()
    await monitor._read(OID_PSE_PORT_ADMIN)

    async def broken_get(
        oid: str, wanted: list[tuple[int, ...]], missing_ok: bool = False
    ) -> dict[tuple[int, ...], Any]:
        calls.append("get")
        raise RuntimeError("row no longer exists")

    good_get = monitor._get_rows
    monitor._get_rows = broken_get  # type: ignore[method-assign]
    calls.clear()
    assert await monitor._read(OID_PSE_PORT_ADMIN) == {1: 1, 2: 2}
    assert calls == ["get", "walk"]

    monitor._get_rows = good_get  # type: ignore[method-assign]
    for _ in range(REWALK_EVERY_READS):
        await monitor._read(OID_PSE_PORT_ADMIN)
    calls.clear()
    await monitor._read(OID_PSE_PORT_ADMIN)
    assert calls == ["walk"]


async def test_read_with_candidates_never_walks() -> None:
    """Candidate rows are fetched directly; missing ones are dropped and cached."""
    monitor, calls = caching_monitor()
    wanted = [(1, 1), (1, 2), (1, 3)]  # (1, 3) does not exist
    assert await monitor._read(OID_PSE_PORT_ADMIN, wanted) == {1: 1, 2: 2}
    assert await monitor._read(OID_PSE_PORT_ADMIN, wanted) == {1: 1, 2: 2}
    assert "walk" not in calls
    assert monitor._rows[OID_PSE_PORT_ADMIN] == [(1, 1), (1, 2)]
