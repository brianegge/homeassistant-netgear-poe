"""Consume switch traps from an snmptrap2mqtt bridge over MQTT.

Alternative to the in-process UDP 162 receiver: a snmptrap2mqtt daemon on
another host receives the traps and publishes normalized JSON to
<base>/<target>/trap (see that project's README for the payload contract).
Messages are matched to this entry by the bridge-captured source IP, which
also fixes the multi-switch ambiguity the local receiver has.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.components import mqtt
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback

from .const import CONF_TRAP_TOPIC, DEFAULT_TRAP_TOPIC
from .trap_receiver import MAX_PHYSICAL_PORT

if TYPE_CHECKING:
    from . import NetgearPoeConfigEntry, NetgearPoeCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_LINK_UP = "linkUp"
EVENT_LINK_DOWN = "linkDown"
EVENT_POE_CHANGE = "poePortChange"


def _resolve_ip(host: str) -> str | None:
    """Resolve the entry host so bridge source IPs match hostname entries."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


async def async_setup_mqtt_traps(
    hass: HomeAssistant,
    entry: NetgearPoeConfigEntry,
    coordinator: NetgearPoeCoordinator,
) -> Callable[[], None] | None:
    """Subscribe to bridge trap topics; returns an unsubscribe callable."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        _LOGGER.warning(
            "MQTT is not available; trap bridge mode disabled for %s "
            "(polling still covers state changes)",
            entry.data[CONF_HOST],
        )
        return None

    base_topic = entry.data.get(CONF_TRAP_TOPIC) or DEFAULT_TRAP_TOPIC
    host = entry.data[CONF_HOST]
    host_ip = await hass.async_add_executor_job(_resolve_ip, host)
    accepted = {value for value in (host, host_ip) if value}
    bridge_offline = False

    @callback
    def _on_trap(msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON trap payload on %s", msg.topic)
            return
        # source_ip is authoritative (captured from the UDP datagram by the
        # bridge); target covers entries configured by name.
        if payload.get("source_ip") not in accepted and payload.get(
            "target"
        ) not in accepted:
            return
        event = payload.get("event_type")
        if_index = payload.get("if_index")
        if event in (EVENT_LINK_UP, EVENT_LINK_DOWN):
            # The bridge is policy-free; the LAG/CPU filter lives here.
            if not isinstance(if_index, int) or if_index > MAX_PHYSICAL_PORT:
                return
            _LOGGER.debug("Bridge trap: port %d %s", if_index, event)
            coordinator.apply_link_trap(if_index, event == EVENT_LINK_UP)
        elif event == EVENT_POE_CHANGE:
            _LOGGER.debug("Bridge trap: PoE change (ifIndex=%s); refreshing", if_index)
            hass.async_create_task(coordinator.async_request_refresh())

    @callback
    def _on_bridge_state(msg: mqtt.ReceiveMessage) -> None:
        nonlocal bridge_offline
        try:
            state = json.loads(msg.payload).get("state")
        except (ValueError, AttributeError):
            return
        if state == "offline" and not bridge_offline:
            bridge_offline = True
            # Log once; the 30 s poll remains the authoritative backstop.
            _LOGGER.warning(
                "snmptrap2mqtt bridge went offline; %s falls back to polling", host
            )
        elif state == "online" and bridge_offline:
            bridge_offline = False
            _LOGGER.info("snmptrap2mqtt bridge is back online")

    unsub_traps = await mqtt.async_subscribe(hass, f"{base_topic}/+/trap", _on_trap)
    unsub_state = await mqtt.async_subscribe(
        hass, f"{base_topic}/bridge/state", _on_bridge_state
    )
    _LOGGER.debug("Subscribed to %s/+/trap for %s", base_topic, host)

    def _unsubscribe() -> None:
        unsub_traps()
        unsub_state()

    return _unsubscribe
