"""Netgear Switch Discovery Protocol (NSDP) scanner.

NSDP is Netgear's proprietary L2 discovery protocol (the ProSAFE utility
uses it). It is a UDP broadcast request/response with a 32-byte header and
TLV records. Two port pairs are in use:

* 63321 -> 63322: "Plus" smart switches (GS1xx/GS3xx/JGSxx). A mixed bag:
  some run web UIs this integration drives (base-UI GS110TP, cheetah
  GS324TP), some are ProSAFE-Plus-only. Discovery probes the web UI
  before offering one of these.
* 63323 -> 63324: "Smart Managed Pro" switches (GS7xx, e.g. GS728TPv2),
  which this integration always controls.

The reply is broadcast, so any non-zero host MAC works (an all-zero host MAC
makes the switch return an error with no data). Switches answer probabilistically,
so a scan repeats bursts over its whole window.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# Any non-zero, locally-administered MAC; the reply is broadcast regardless.
_HOST_MAC = bytes.fromhex("020000000001")
_SIGNATURE = b"NSDP"

# (listen port, send port, is_pro)
_PORT_PAIRS = ((63321, 63322, False), (63323, 63324, True))
_BROADCAST_ADDRS = ("255.255.255.255",)

_TAG_MODEL = 0x0001
_TAG_NAME = 0x0003
_TAG_MAC = 0x0004
_TAG_IP = 0x0006
_TAG_FIRMWARE = 0x000D
_TAG_END = 0xFFFF

_TEXT_TAGS = {_TAG_MODEL, _TAG_NAME, _TAG_FIRMWARE}
_REQUEST_TAGS = (_TAG_MODEL, _TAG_NAME, _TAG_MAC, _TAG_IP, _TAG_FIRMWARE)


@dataclass(frozen=True)
class NsdpSwitch:
    """A switch discovered via NSDP."""

    mac: str
    host: str
    model: str
    name: str
    firmware: str
    is_pro: bool


def _build_request(seq: int) -> bytes:
    """Build an NSDP read request for the discovery attributes."""
    header = (
        struct.pack("!BBH", 1, 1, 0)  # version 1, op 1 (read request), result 0
        + b"\x00" * 4
        + _HOST_MAC
        + b"\x00" * 6  # device MAC = any
        + b"\x00" * 2
        + struct.pack("!H", seq)
        + _SIGNATURE
        + b"\x00" * 4
    )
    body = b"".join(struct.pack("!HH", tag, 0) for tag in _REQUEST_TAGS)
    return header + body + struct.pack("!HH", _TAG_END, 0)


def _parse_response(data: bytes, is_pro: bool) -> NsdpSwitch | None:
    """Parse an NSDP read response into an NsdpSwitch, or None."""
    if len(data) < 32 or data[24:28] != _SIGNATURE or data[1] != 2:
        return None
    fields: dict[int, str] = {}
    index = 32
    while index + 4 <= len(data):
        tag, length = struct.unpack("!HH", data[index : index + 4])
        index += 4
        if tag == _TAG_END:
            break
        value = data[index : index + length]
        index += length
        if tag == _TAG_MAC and length == 6:
            fields[tag] = ":".join(f"{b:02x}" for b in value)
        elif tag == _TAG_IP and length == 4:
            fields[tag] = ".".join(str(b) for b in value)
        elif tag in _TEXT_TAGS:
            fields[tag] = value.split(b"\x00")[0].decode("latin1", "replace").strip()
    mac = fields.get(_TAG_MAC)
    host = fields.get(_TAG_IP)
    if not mac or not host:
        return None
    return NsdpSwitch(
        mac=mac,
        host=host,
        model=fields.get(_TAG_MODEL, ""),
        name=fields.get(_TAG_NAME, ""),
        firmware=fields.get(_TAG_FIRMWARE, ""),
        is_pro=is_pro,
    )


class _NsdpProtocol(asyncio.DatagramProtocol):
    """Collects NSDP replies for one port pair."""

    def __init__(self, is_pro: bool, found: dict[str, NsdpSwitch]) -> None:
        self._is_pro = is_pro
        self._found = found

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        switch = _parse_response(data, self._is_pro)
        if switch is not None:
            # A Pro-port answer wins over a Plus-port answer for the same MAC.
            existing = self._found.get(switch.mac)
            if existing is None or (switch.is_pro and not existing.is_pro):
                self._found[switch.mac] = switch

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("NSDP socket error: %s", exc)


async def async_discover(
    duration: float = 30.0, burst_interval: float = 5.0
) -> list[NsdpSwitch]:
    """Broadcast NSDP requests for `duration` seconds and return switches found.

    Repeats request bursts every `burst_interval` seconds because switches
    answer probabilistically and can take up to ~a minute to all respond.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, NsdpSwitch] = {}
    transports: list[tuple[asyncio.DatagramTransport, int]] = []

    for listen_port, send_port, is_pro in _PORT_PAIRS:
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda pro=is_pro: _NsdpProtocol(pro, found),
                local_addr=("0.0.0.0", listen_port),
                allow_broadcast=True,
                reuse_port=True,
            )
        except OSError as err:
            _LOGGER.warning("NSDP cannot bind UDP %d: %s", listen_port, err)
            continue
        transports.append((transport, send_port))

    if not transports:
        return []

    try:
        seq = 1
        deadline = loop.time() + duration
        while loop.time() < deadline:
            for transport, send_port in transports:
                for addr in _BROADCAST_ADDRS:
                    try:
                        transport.sendto(_build_request(seq), (addr, send_port))
                        seq += 1
                    except OSError as err:
                        _LOGGER.debug("NSDP send failed: %s", err)
            await asyncio.sleep(min(burst_interval, max(0.0, deadline - loop.time())))
    finally:
        for transport, _ in transports:
            transport.close()

    return list(found.values())
