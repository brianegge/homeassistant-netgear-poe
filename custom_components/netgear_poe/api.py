"""HTTP client for Netgear Smart Managed Pro switches (Realtek RTL83xx web API).

The GS728TPv2 (firmware 6.x) exposes a JSON CGI API at /cgi/get.cgi and
/cgi/set.cgi. Authentication posts an obfuscated password to
cmd=home_loginAuth, then all writes carry an RSA-encrypted session token in
the X-CSRF-XSID header. Protocol reference: https://github.com/tai/gs310tp
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from hashlib import md5
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)


class NetgearError(Exception):
    """Request to the switch failed."""


class NetgearAuthError(NetgearError):
    """Authentication failed."""


def encode_password(password: str) -> str:
    """Obfuscate the password into Netgear's 320-char login format.

    Password characters are placed in reverse order at every 7th position,
    the length is embedded at fixed offsets, and the rest is random filler.
    """
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    buf = [secrets.choice(chars) for _ in range(320)]
    for i, ch in enumerate(reversed(password)):
        buf[6 + 7 * i] = ch
    buf[122] = str(len(password) // 10)
    buf[288] = str(len(password) % 10)
    return "".join(buf)


def rsa_encrypt(message: str, exponent_hex: str, modulus_hex: str) -> str:
    """RSA-encrypt with PKCS#1 v1.5 padding, compatible with jsbn rsa.js.

    Returns base64 of the big-endian ciphertext, as the switch expects in
    the X-CSRF-XSID header.
    """
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    k = (n.bit_length() + 7) // 8

    data = message.encode()
    if len(data) > k - 11:
        raise NetgearError("RSA message too long")
    padding = bytes(secrets.randbelow(255) + 1 for _ in range(k - 3 - len(data)))
    block = b"\x00\x02" + padding + b"\x00" + data
    cipher = pow(int.from_bytes(block, "big"), e, n)
    return b64encode(cipher.to_bytes(k, "big")).decode()


def form_body(fields: dict[str, Any]) -> str:
    """Build the odd JSON body the API expects: {"_ds=1&k=v&_de=1":{}}."""
    parts = "&".join(f"{k}={v}" for k, v in fields.items())
    return '{"_ds=1&' + parts + '&_de=1":{}}'


@dataclass
class SwitchInfo:
    """Identity of the switch."""

    name: str
    model: str
    firmware: str = ""
    # SNMP sysObjectID (e.g. "1.3.6.1.4.1.4526.100.4.29"); a model-exact key.
    sys_object_id: str = ""


@dataclass
class PoePort:
    """State of a single PoE port."""

    port: int
    admin_enabled: bool
    detection_status: str = "searching"
    power_watts: float | None = None
    alias: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PoeData:
    """State of the switch."""

    ports: dict[int, PoePort] = field(default_factory=dict)
    consumption_watts: float | None = None
    # Per-port link state from SNMP; empty when SNMP is unavailable
    link: dict[int, bool] = field(default_factory=dict)


@dataclass
class DualImageStatus:
    """What each firmware slot holds and which one the switch is running.

    Both firmware-capable generations keep two flash slots and boot whichever
    is marked next-active, so an upgrade writes to the inactive one and the
    running image stays as a rollback.
    """

    versions: dict[str, str]  # {"image1": "5.4.2.35", "image2": "5.4.2.33"}
    current_active: str  # the slot the switch booted from
    next_active: str  # the slot it will boot next

    @property
    def inactive(self) -> str:
        """The slot that is safe to overwrite."""
        return "image2" if self.current_active == "image1" else "image1"


class NetgearPoeApi:
    """Async client for PoE control over the switch's web API."""

    # No firmware-install path is implemented for this backend (yet).
    supports_firmware_install = False

    def __init__(
        self,
        host: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.host = host
        self._password = password
        self._session = session
        self._owns_session = session is None
        self._xsid_header: str | None = None
        self._login_lock = asyncio.Lock()
        self._port_names: dict[int, str] = {}
        self._poll_count = 0
        # Fetch port names over the web CGI; disabled when SNMP (ifAlias) is
        # the name source, to avoid an extra web login per refresh.
        self.web_port_names_enabled = True

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    def _url(self, cgi: str, cmd: str) -> str:
        query = f"cmd={cmd}&dummy={int(time.time() * 1000)}"
        checksum = md5(query.encode()).hexdigest()
        return f"http://{self.host}/cgi/{cgi}?{query}&hash={checksum}"

    async def _request(
        self, cgi: str, cmd: str, body: str | None = None
    ) -> dict[str, Any]:
        session = self._get_session()
        headers = {}
        if self._xsid_header:
            headers["X-CSRF-XSID"] = self._xsid_header
        try:
            if body is None:
                resp = await session.get(self._url(cgi, cmd), headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                resp = await session.post(
                    self._url(cgi, cmd), data=body, headers=headers
                )
            resp.raise_for_status()
            return await resp.json(content_type=None)
        except ValueError as err:
            # e.g. an HTML login page from firmware this driver doesn't speak
            raise NetgearError(f"Non-JSON response to {cmd}: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Request {cmd} failed: {err}") from err

    async def async_login(self) -> None:
        """Authenticate and store the CSRF session header."""
        self._xsid_header = None
        result = await self._request(
            "set.cgi",
            "home_loginAuth",
            form_body({"pwd": encode_password(self._password)}),
        )
        if result.get("status") != "ok":
            raise NetgearAuthError(f"Login rejected: {result}")

        for _ in range(5):
            status = await self._request("get.cgi", "home_loginStatus")
            data = status.get("data", {})
            if data.get("status") == "ok" and data.get("sess"):
                sess = b64decode(data["sess"]).decode()
                tabid, expo, modulus = sess[:32], sess[32:37], sess[37:-1]
                self._xsid_header = rsa_encrypt(tabid, expo, modulus)
                return
            await asyncio.sleep(1)
        raise NetgearAuthError("Login failed: no session granted (wrong password?)")

    async def _authed_request(
        self, cgi: str, cmd: str, body: str | None = None
    ) -> dict[str, Any]:
        """Request with automatic (re)login."""
        async with self._login_lock:
            if self._xsid_header is None:
                await self.async_login()
        result = await self._request(cgi, cmd, body)
        if _is_auth_failure(result):
            async with self._login_lock:
                await self.async_login()
            result = await self._request(cgi, cmd, body)
            if _is_auth_failure(result):
                raise NetgearAuthError(f"Re-login failed for {cmd}: {result}")
        return result

    async def _async_fetch_port_names(self, retries: int = 0) -> dict[int, str]:
        """Return {port: assigned description} from the port config page.

        Entity names are set from the first poll, so the initial fetch retries
        to ride out a transient switch error (login throttle, a stray 502).
        """
        last_exc: NetgearError | None = None
        for attempt in range(retries + 1):
            try:
                result = await self._authed_request("get.cgi", "port_port")
            except NetgearError as err:
                last_exc = err
            else:
                names: dict[int, str] = {}
                for index, row in enumerate(result.get("data", {}).get("ports", [])):
                    port = int(row.get("ifindex", index + 1))
                    descp = str(row.get("descp", "")).strip()
                    if descp:
                        names[port] = descp
                if names:
                    return names
            if attempt < retries:
                await asyncio.sleep(1.5)
        if last_exc is not None:
            raise last_exc
        return {}

    async def async_get_data(self) -> PoeData:
        """Fetch PoE state for all ports."""
        result = await self._authed_request("get.cgi", "poe_port")
        rows = _port_rows(result)
        if not rows:
            raise NetgearError(f"No PoE ports in poe_port response: {result}")

        # Port names rarely change; refresh them on the first poll (with
        # retries so entities come up named) and occasionally thereafter to
        # pick up renames without a reload.
        if self.web_port_names_enabled and (
            not self._port_names or self._poll_count % 20 == 0
        ):
            initial = not self._port_names
            try:
                names = await self._async_fetch_port_names(retries=3 if initial else 0)
                if names:
                    self._port_names = names
            except NetgearError:
                if initial:
                    _LOGGER.warning(
                        "Could not fetch port names from %s; ports will be "
                        "unnamed until the next refresh",
                        self.host,
                    )
        self._poll_count += 1

        data = PoeData()
        total = 0.0
        have_power = False
        for index, row in enumerate(rows):
            port = int(row.get("port", index + 1))
            power = _row_power_watts(row)
            if power is not None:
                total += power
                have_power = True
            data.ports[port] = PoePort(
                port=port,
                admin_enabled=_row_enabled(row),
                detection_status=_parse_lang_key(
                    str(row.get("status", "unknown")), "txtPortStatus"
                ).lower(),
                power_watts=power,
                alias=self._port_names.get(port, ""),
                raw=row,
            )
        if have_power:
            data.consumption_watts = round(total, 1)
        return data

    async def async_set_port_enabled(self, port: int, enabled: bool) -> None:
        """Enable or disable PoE on a port, preserving its other settings."""
        data = await self.async_get_data()
        if port not in data.ports:
            raise NetgearError(f"Port {port} not found")
        fields = _set_fields(data.ports[port].raw, port)
        fields["state"] = 1 if enabled else 0
        result = await self._authed_request("set.cgi", "poe_port", form_body(fields))
        if result.get("status") != "ok":
            raise NetgearError(f"PoE set failed: {result}")

    async def async_power_cycle_port(self, port: int) -> None:
        """Power cycle a port using the switch's native PoE reset."""
        data = await self.async_get_data()
        if port not in data.ports:
            raise NetgearError(f"Port {port} not found")
        fields = _set_fields(data.ports[port].raw, port)
        fields["state"] = 1
        result = await self._authed_request(
            "set.cgi", "poe_portReset", form_body(fields)
        )
        if result.get("status") != "ok":
            raise NetgearError(f"PoE reset failed: {result}")

    async def async_set_port_name(self, port: int, name: str) -> None:
        """Set a port's description (the switch UI name / SNMP ifAlias).

        The edit form echoes the port's link settings alongside the new
        description, so the current port_port row is fetched first.
        """
        result = await self._authed_request("get.cgi", "port_port")
        row: dict[str, Any] | None = None
        for index, candidate in enumerate(result.get("data", {}).get("ports", [])):
            if int(candidate.get("ifindex", index + 1)) == port:
                row = candidate
                break
        if row is None:
            raise NetgearError(f"Port {port} not found")
        fields = {
            "portList": quote(str(row.get("portName", port)), safe=""),
            "descp": quote(name, safe=""),
            "adminStatus": _port_edit_value(row, "adminStatus"),
            "adminSpeed": _port_edit_value(row, "adminSpeed"),
            "adminDuplex": _port_edit_value(row, "adminDuplex"),
            "adminFlowCtrl": _port_edit_value(row, "adminFlowCtrl"),
            "xsrf": "undefined",
        }
        result = await self._authed_request(
            "set.cgi", "port_portEdit", form_body(fields)
        )
        if result.get("status") != "ok":
            raise NetgearError(f"Port name set failed: {result}")
        if name:
            self._port_names[port] = name
        else:
            self._port_names.pop(port, None)

    async def async_ensure_trap_destination(self, dest_ip: str, community: str) -> None:
        """Register an enabled v2c trap destination on the switch.

        Field names/values match the switch's Trap Configuration form:
        version 1 = SNMPv2, snmpStatus 1 = Enable. The switch tolerates a
        duplicate recipient IP, so this is safe to call on every setup.
        linkUpDown/PoE trap flags are left as configured on the switch.
        """
        await self._authed_request(
            "set.cgi",
            "snmp_trapConfgAdd",
            form_body(
                {
                    "recipientsIP": dest_ip,
                    "version": 1,
                    "communityStr": community,
                    "snmpStatus": 1,
                }
            ),
        )

    async def async_get_info(self) -> SwitchInfo:
        """Return the switch's name, model and firmware version."""
        result = await self._authed_request("get.cgi", "sys_info")
        data = result.get("data", {})
        model = _parse_lang_key(str(data.get("sysProduct", "")), "txtModelDescp")
        return SwitchInfo(
            name=str(data.get("sysName", "")),
            model=model,
            firmware=str(data.get("fwVer", "")),
            sys_object_id=str(data.get("sysObjectID", "")).lstrip("."),
        )

    async def async_logout(self) -> None:
        """Log out to free the switch's limited session slots."""
        if self._xsid_header is None:
            return
        try:
            await self._request("set.cgi", "home_logout", form_body({}))
        except NetgearError:
            _LOGGER.debug("Logout request failed", exc_info=True)
        self._xsid_header = None

    async def async_close(self) -> None:
        """Log out and close the HTTP session if we own it."""
        await self.async_logout()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


def _is_auth_failure(result: dict[str, Any]) -> bool:
    status = str(result.get("status", "")).lower()
    msg = str(result.get("msgType", "")).lower()
    return (
        status in ("unauth", "unauthorized")
        or "login" in msg
        or (status == "err" and "auth" in str(result).lower())
    )


def _port_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the list of per-port dicts from a poe_port response."""
    data = result.get("data", result)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _row_enabled(row: dict[str, Any]) -> bool:
    state = row.get("state", row.get("adminState", row.get("enable", 0)))
    if isinstance(state, str):
        return state.strip().lower() in ("1", "on", "enable", "enabled", "true")
    return bool(int(state))


def _parse_lang_key(value: str, prefix: str) -> str:
    """Turn lang('poe','txtPortStatusDelivering') into 'Delivering'."""
    if "'" not in value:
        return value
    key = value.rsplit("'", 2)[-2]
    if key.startswith(prefix):
        key = key[len(prefix) :]
    return key


def _port_edit_value(row: dict[str, Any], field: str) -> str:
    """Map a port_port row value to the label the port edit form posts.

    Rows carry lang() artifacts like lang('common','lblAuto') or, for the
    admin status, numeric flags; the set form wants lowercase labels ("on",
    "auto", "disable"). The edit form echoes every link setting back, so a
    value this can't read is an error: defaulting it would silently rewrite
    the port's configuration on a rename.
    """
    value = row.get(field)
    if value in (None, ""):
        raise NetgearError(f"Port settings incomplete ({field} missing); not renaming")
    text = str(value)
    if text.isdigit():
        # Only the admin status is a plain on/off flag; a numeric speed,
        # duplex or flow-control code has no safe label to map to.
        if field == "adminStatus":
            return "on" if int(text) else "disable"
        raise NetgearError(f"Port settings unreadable ({field}={text!r}); not renaming")
    label = _parse_lang_key(text, "lbl").lower()
    return {"enabled": "on", "enable": "on", "disabled": "disable"}.get(label, label)


def _row_power_watts(row: dict[str, Any]) -> float | None:
    """Return port power in watts; the switch reports milliwatts."""
    if "power" not in row:
        return None
    try:
        return round(float(row["power"]) / 1000, 1)
    except (TypeError, ValueError):
        return None


def _set_fields(raw: dict[str, Any], port: int) -> dict[str, Any]:
    """Build the poe_port set fields, echoing current settings."""
    return {
        "state": (_row_enabled(raw) and 1) or 0,
        "priority": raw.get("priority", 0),
        "powerMode": raw.get("powerMode", 3),
        "powerLimitMode": raw.get("powerLimitMode", 2),
        "adminPower": raw.get("adminPower", 30000),
        "detectMode": raw.get("detectMode", 0),
        "sched": "%3F",
        "selEntry": port - 1,
        "xsrf": "undefined",
    }
