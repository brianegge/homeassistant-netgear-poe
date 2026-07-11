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


class NetgearPoeApi:
    """Async client for PoE control over the switch's web API."""

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

    async def async_get_data(self) -> PoeData:
        """Fetch PoE state for all ports."""
        result = await self._authed_request("get.cgi", "poe_port")
        rows = _port_rows(result)
        if not rows:
            raise NetgearError(f"No PoE ports in poe_port response: {result}")

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
        result = await self._authed_request(
            "set.cgi", "poe_port", form_body(fields)
        )
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

    async def async_get_info(self) -> tuple[str, str]:
        """Return (sysName, model) from the switch."""
        result = await self._authed_request("get.cgi", "sys_info")
        data = result.get("data", {})
        model = _parse_lang_key(str(data.get("sysProduct", "")), "txtModelDescp")
        return str(data.get("sysName", "")), model

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
    return status in ("unauth", "unauthorized") or "login" in msg or (
        status == "err" and "auth" in str(result).lower()
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
        "state": _row_enabled(raw) and 1 or 0,
        "priority": raw.get("priority", 0),
        "powerMode": raw.get("powerMode", 3),
        "powerLimitMode": raw.get("powerLimitMode", 2),
        "adminPower": raw.get("adminPower", 30000),
        "detectMode": raw.get("detectMode", 0),
        "sched": "%3F",
        "selEntry": port - 1,
        "xsrf": "undefined",
    }
