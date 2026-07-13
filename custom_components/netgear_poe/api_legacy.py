"""Client for older Netgear Smart Managed Pro switches (Marvell "xui" web UI).

Models like the GS516TP (firmware 6.0.x, Marvell-based) predate the JSON
CGI API in api.py. Their web UI is served under a per-device path prefix
(e.g. /csbe116353/) and speaks XML:

* Every request to / redirects (302) to /<prefix>/ — that redirect is how
  both the prefix and the legacy firmware itself are detected.
* Login: GET /<prefix>/System.xml?action=login&user=admin&password=<pw>
  answers ``<statusCode>`` (0/9/10/12/13/14 = success) and a ``sessionID``
  response header that becomes the session cookie. Idle timeout is short
  (~5 minutes), so requests re-login on a 302.
* Data: GET /<prefix>/wcd?{SectionName} returns a DeviceConfiguration XML
  document (curly braces are sent literally; the GoAhead server wants them).
* Writes: POST /<prefix>/wcd with an XML body:
  <DeviceConfiguration set="set"><PoEPSEInterfaceList action="set" ...>
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import aiohttp
from yarl import URL

from .api import NetgearAuthError, NetgearError, NetgearPoeApi, PoeData, PoePort

_LOGGER = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r"/(csbe\d+)/")
_LOGIN_OK_CODES = {"0", "9", "10", "12", "13", "14"}
_DETECTION_STATUS = {
    "1": "disabled",
    "2": "searching",
    "3": "delivering",
    "4": "fault",
    "5": "test",
    "6": "otherfault",
}
_POWER_CYCLE_OFF_SECONDS = 3


def _escape_password(password: str) -> str:
    """Escape the password exactly the way the login page's JS does."""
    replacements = {"%": "%25", "#": "%23", "&": "%26", "+": "%2B", " ": "%20"}
    return "".join(replacements.get(ch, ch) for ch in password)


def _parse_xml(text: str) -> ET.Element:
    """Parse a wcd/System.xml response, tolerating leading whitespace/junk."""
    start = text.find("<?xml")
    if start < 0:
        raise NetgearError("Switch returned a non-XML response")
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError as err:
        raise NetgearError(f"Could not parse switch XML: {err}") from err


class NetgearLegacyApi:
    """Async PoE client for the legacy xui web UI. Mirrors NetgearPoeApi."""

    def __init__(
        self,
        host: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
        prefix: str | None = None,
    ) -> None:
        self.host = host
        self._password = password
        self._session = session
        self._owns_session = session is None
        self._prefix = prefix
        self._cookie: str | None = None
        self._login_lock = asyncio.Lock()
        # Interface names ("g5") keyed by port number, learned from polls.
        self._if_names: dict[int, str] = {}
        # Present for interface parity with NetgearPoeApi; the legacy UI has
        # no separate port-name fetch, so this is unused.
        self.web_port_names_enabled = True

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    def _url(self, path_and_query: str) -> URL:
        # encoded=True keeps the literal {braces} the wcd endpoint requires.
        # These switches serve plain HTTP only; they have no TLS support.
        return URL(
            f"http://{self.host}/{self._prefix}/{path_and_query}",  # NOSONAR
            encoded=True,
        )

    async def _async_refresh_prefix(self) -> None:
        """(Re)derive the per-device path prefix from the root redirect."""
        session = self._get_session()
        try:
            resp = await session.get(
                f"http://{self.host}/",  # NOSONAR — the switch is HTTP-only
                allow_redirects=False,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Cannot connect to {self.host}: {err}") from err
        match = _PREFIX_RE.search(resp.headers.get("Location", ""))
        if match is None:
            raise NetgearError(
                f"{self.host} did not redirect to a legacy UI prefix"
            )
        self._prefix = match.group(1)

    async def async_login(self) -> None:
        """Authenticate and store the session cookie."""
        self._cookie = None
        await self._async_refresh_prefix()
        session = self._get_session()
        url = self._url(
            "System.xml?action=login&user=admin"
            f"&password={_escape_password(self._password)}"
        )
        try:
            resp = await session.get(url, allow_redirects=False)
            text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Login request failed: {err}") from err
        code = _parse_xml(text).findtext(".//statusCode", "").strip()
        if code not in _LOGIN_OK_CODES:
            raise NetgearAuthError(f"Login rejected (statusCode={code or '?'})")
        # Header form: "UserId=<ip>&<id>&;path=/" — the cookie is the part
        # before ";path=".
        session_id = resp.headers.get("sessionID", "").split(";path=")[0]
        if not session_id:
            raise NetgearAuthError("Login gave no sessionID header")
        self._cookie = session_id

    async def _attempt_request(
        self, path_and_query: str, body: str | None
    ) -> str | None:
        """One HTTP attempt; None means the session was rejected."""
        session = self._get_session()
        headers = {"Cookie": f"sessionID={self._cookie}"}
        try:
            if body is None:
                resp = await session.get(
                    self._url(path_and_query),
                    headers=headers,
                    allow_redirects=False,
                )
            else:
                headers["Content-Type"] = "text/xml"
                resp = await session.post(
                    self._url(path_and_query),
                    data=body,
                    headers=headers,
                    allow_redirects=False,
                )
            # An expired session answers 302 to the login page.
            if resp.status == 302:
                return None
            resp.raise_for_status()
            return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            # A rejected session can also surface as a parse error: the
            # switch echoes the stale cookie as a malformed header line
            # ("sessionID=..." with no colon), which aiohttp's strict
            # parser refuses before we ever see the 302.
            if "sessionID" in str(err):
                return None
            raise NetgearError(f"Request {path_and_query} failed: {err}") from err

    async def _request(
        self, path_and_query: str, body: str | None = None
    ) -> ET.Element:
        """Perform a request, logging in (again) as needed."""
        for _attempt in (0, 1):
            if self._cookie is None:
                async with self._login_lock:
                    if self._cookie is None:
                        await self.async_login()
            text = await self._attempt_request(path_and_query, body)
            if text is not None:
                return _parse_xml(text)
            self._cookie = None
        raise NetgearAuthError("Session rejected after re-login")

    async def async_get_info(self) -> tuple[str, str]:
        """Return (sysName, model) from the switch."""
        root = await self._request("wcd?{DeviceBasicInfo}")
        name = root.findtext(".//DeviceBasicInfo/deviceName", "").strip()
        model = root.findtext(".//DeviceBasicInfo/deviceDescription", "").strip()
        return name, model

    async def async_get_data(self) -> PoeData:
        """Fetch PoE state for all ports."""
        root = await self._request("wcd?{PoEPSEInterfaceList}")
        interfaces = root.findall(".//PoEPSEInterfaceList/Interface")
        if not interfaces:
            raise NetgearError("No PoE ports in PoEPSEInterfaceList response")

        data = PoeData()
        total = 0.0
        for iface in interfaces:
            text = {child.tag: (child.text or "").strip() for child in iface}
            try:
                port = int(text.get("interfaceID", ""))
            except ValueError:
                continue
            self._if_names[port] = text.get("interfaceName", f"g{port}")
            try:
                power = round(float(text.get("outputPower", "0")) / 1000, 1)
            except ValueError:
                power = 0.0
            total += power
            data.ports[port] = PoePort(
                port=port,
                admin_enabled=text.get("adminEnable") == "1",
                detection_status=_DETECTION_STATUS.get(
                    text.get("detectionStatus", ""), "unknown"
                ),
                power_watts=power,
                alias=text.get("poweredDevice", ""),
                raw=text,
            )
        data.consumption_watts = round(total, 1)
        return data

    async def _async_if_name(self, port: int) -> str:
        if port not in self._if_names:
            await self.async_get_data()
        if port not in self._if_names:
            raise NetgearError(f"Port {port} not found")
        return self._if_names[port]

    async def async_set_port_enabled(self, port: int, enabled: bool) -> None:
        """Enable or disable PoE on a port."""
        if_name = await self._async_if_name(port)
        body = (
            "<?xml version='1.0' encoding='utf-8'?>"
            '<DeviceConfiguration set="set">'
            '<PoEPSEInterfaceList action="set" set="set">'
            f"<Interface><interfaceName>{if_name}</interfaceName>"
            # SNMP TruthValue semantics (pethPsePortAdminEnable): 2 = disable
            f"<adminEnable>{1 if enabled else 2}</adminEnable></Interface>"
            "</PoEPSEInterfaceList></DeviceConfiguration>"
        )
        root = await self._request("wcd", body=body)
        code = root.findtext(".//ActionStatus/statusCode", "").strip()
        if code not in ("", "0"):
            status = root.findtext(".//ActionStatus/statusString", "").strip()
            raise NetgearError(f"PoE set failed: {status or code}")

    async def async_power_cycle_port(self, port: int) -> None:
        """Power cycle a port; the legacy UI has no native PoE reset."""
        await self.async_set_port_enabled(port, False)
        try:
            await asyncio.sleep(_POWER_CYCLE_OFF_SECONDS)
        finally:
            # Restore power even if the sleep is cancelled mid-cycle, and
            # retry so a transient error can't leave the device unpowered.
            for attempt in range(3):
                try:
                    await self.async_set_port_enabled(port, True)
                    break
                except NetgearError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)

    async def async_ensure_trap_destination(self, dest_ip: str, community: str) -> None:
        """Trap registration is not implemented for the legacy UI."""
        raise NetgearError(
            "SNMP trap registration is not supported on this switch model"
        )

    async def async_close(self) -> None:
        """Close the HTTP session if we own it."""
        self._cookie = None
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


async def async_detect_api(
    host: str,
    password: str,
    session: aiohttp.ClientSession | None = None,
) -> NetgearPoeApi | NetgearLegacyApi:
    """Return the right client for the switch's firmware generation.

    Legacy xui firmware 302-redirects every request to its /csbe<id>/
    prefix; the newer JSON-CGI firmware serves the login page directly.
    """
    timeout = aiohttp.ClientTimeout(total=10)
    probe_session = session or aiohttp.ClientSession(timeout=timeout)
    try:
        try:
            resp = await probe_session.get(
                f"http://{host}/",  # NOSONAR — probing the HTTP-only web UI
                allow_redirects=False,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Cannot connect to {host}: {err}") from err
        match = _PREFIX_RE.search(resp.headers.get("Location", ""))
    finally:
        if session is None:
            await probe_session.close()

    if match is not None:
        return NetgearLegacyApi(
            host=host, password=password, session=session, prefix=match.group(1)
        )
    return NetgearPoeApi(host=host, password=password, session=session)
