"""Client for Netgear smart switches with the classic "/base/" web UI.

Models like the GS110TP (firmware 5.4.2.x, Broadcom-based) predate both the
JSON CGI API in api.py and the xui XML API in api_legacy.py. They serve a
frames-based HTML UI under /base/ and are driven by posting the same forms a
browser would:

* Login: POST /base/main_login.html with ``pwd``. Success sets a ``SID``
  cookie; a bad password returns the login page with ``err_flag=1``.
* Reads: GET a page and scrape its table — poe/poe_port_cfg.html for per-port
  PoE state, poe/poe_cfg.html for total consumption, system/management/
  sysInfo.html for identity, system/port/port_cfg.html for port names.
* Writes: POST the page's form with ``selectedPorts`` naming the target port
  ("g4;") and ``submt=16`` (the 0x10 the UI's JS sets). Fields left at their
  "unchanged" sentinel keep the port's other settings: "Blank" on the PoE
  form, "None" on the port-config form.
* An expired session serves the login page in place of the requested page,
  which is how re-login is triggered.

Tables are parsed by column *header* rather than by fixed offsets, since a
port with no description renders an empty cell and firmware revisions move
columns around.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape

import aiohttp

from .api import (
    NetgearAuthError,
    NetgearError,
    PoeData,
    PoePort,
    SwitchInfo,
)

_LOGGER = logging.getLogger(__name__)

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_PORT_RE = re.compile(r"^g(\d+)$")
_LOGIN_FORM_RE = re.compile(r'name="pwd"', re.I)

# The value the UI's JS writes into the hidden "submt" field (0x10).
_SUBMIT = "16"
_POWER_CYCLE_OFF_SECONDS = 3
# The pages declare iso-8859-1 in a meta tag but send no charset header.
# Decoding explicitly keeps it deterministic (and can never raise) instead of
# leaving aiohttp to sniff the encoding of every poll's response.
_ENCODING = "iso-8859-1"

# "Status" column text -> the vocabulary the other backends report.
_DETECTION_STATUS = {
    "disabled": "disabled",
    "searching": "searching",
    "delivering power": "delivering",
    "fault": "fault",
    "test": "test",
    "other fault": "otherfault",
}


def _cell_text(cell: str) -> str:
    """Strip tags/entities from a table cell and collapse its whitespace."""
    return " ".join(unescape(_TAG_RE.sub("", cell)).split())


def _rows(html: str) -> list[list[str]]:
    """Return every table row on the page as a list of cell texts."""
    return [
        [_cell_text(cell) for cell in _CELL_RE.findall(row)]
        for row in _ROW_RE.findall(html)
    ]


def _port_rows(html: str, required: tuple[str, ...]) -> list[dict[str, str]]:
    """Return the per-port rows of the table whose header has `required`.

    Rows are keyed by header text. The header is matched on several of its
    columns so a stray "Port" cell elsewhere on the page can't hijack it.
    """
    headers: list[str] | None = None
    port_index = -1
    out: list[dict[str, str]] = []
    for cells in _rows(html):
        if headers is None:
            if all(name in cells for name in required):
                headers = cells
                port_index = cells.index("Port")
            continue
        # Skip the edit row (selects) and anything that isn't a "g<n>" port.
        if port_index < len(cells) and _PORT_RE.match(cells[port_index]):
            # Not strict: the port-config header stops at "Maximum" while its
            # data rows carry extra trailing cells. The columns that matter
            # line up, so the surplus is dropped on purpose.
            out.append(dict(zip(headers, cells, strict=False)))
    return out


def _labeled_value(rows: list[list[str]], label: str) -> str:
    """Return the value cell of a two-column "<label> | <value>" row."""
    for cells in rows:
        if len(cells) >= 2 and cells[0] == label:
            return cells[1]
    return ""


def _row_under_header(rows: list[list[str]], header: str) -> dict[str, str]:
    """Return the first data row beneath a header row containing `header`.

    Headers are anchored at `header` because these tables prepend a spacer
    cell to the header row that the data rows below it don't have.
    """
    for index, cells in enumerate(rows):
        if header not in cells:
            continue
        headers = cells[cells.index(header) :]
        for later in rows[index + 1 :]:
            if len(later) == len(headers):
                return dict(zip(headers, later, strict=True))
        break
    return {}


def _input_value(html: str, name: str) -> str:
    """Return the VALUE of a named <INPUT>, e.g. the sysName text box."""
    match = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', html, re.I)
    if match is None:
        return ""
    value = re.search(r'value="([^"]*)"', match.group(0), re.I)
    return unescape(value.group(1)).strip() if value else ""


def _float_or(text: str, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _parse_port_names(html: str) -> dict[int, str] | None:
    """Return {port: description}, or None if the table wasn't found.

    An empty dict is a real answer — no port has a description yet — and is
    kept distinct from a page that couldn't be parsed at all.
    """
    rows = _port_rows(html, ("Port", "Description", "Link Status"))
    if not rows:
        return None
    names: dict[int, str] = {}
    for row in rows:
        match = _PORT_RE.match(row.get("Port", ""))
        descr = row.get("Description", "").strip()
        if match and descr:
            names[int(match.group(1))] = descr
    return names


class NetgearBaseUiApi:
    """Async PoE client for the classic /base/ web UI. Mirrors NetgearPoeApi."""

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
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self._port_names: dict[int, str] = {}
        self._poll_count = 0
        # Fetch port names from port_cfg.html; disabled when SNMP (ifAlias) is
        # the name source, to avoid an extra page fetch per refresh.
        self.web_port_names_enabled = True

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    def _url(self, path: str) -> str:
        # These switches serve plain HTTP only; they have no TLS support.
        return f"http://{self.host}{path}"  # NOSONAR

    async def async_login(self) -> None:
        """Authenticate and store the SID session cookie."""
        self._logged_in = False
        session = self._get_session()
        try:
            async with session.post(
                self._url("/base/main_login.html"),
                data={
                    "pwd": self._password,
                    "login.x": "0",
                    "login.y": "0",
                    "err_flag": "0",
                    "err_msg": "",
                },
            ) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding=_ENCODING)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Login request failed: {err}") from err

        # The switch signals a good login with a SID cookie and a bad one by
        # re-serving the login page with err_flag=1.
        if not any(cookie.key == "SID" for cookie in session.cookie_jar):
            reason = _input_value(text, "err_msg") or "wrong password?"
            raise NetgearAuthError(f"Login rejected: {reason}")
        self._logged_in = True

    async def _attempt_request(
        self, path: str, data: dict[str, str] | None
    ) -> str | None:
        """One HTTP attempt; None means the session was rejected.

        The session is long-lived, so the response is entered as a context
        manager: raise_for_status() releases the connection itself, but a
        failure part-way through reading the body would not.
        """
        session = self._get_session()
        url = self._url(path)
        request = (
            session.get(url)
            if data is None
            else session.post(url, data=data, headers={"Referer": url})
        )
        try:
            async with request as resp:
                resp.raise_for_status()
                text = await resp.text(encoding=_ENCODING)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Request {path} failed: {err}") from err
        # An expired session silently serves the login page instead.
        if _LOGIN_FORM_RE.search(text):
            return None
        return text

    async def _request(self, path: str, data: dict[str, str] | None = None) -> str:
        """Perform a request, logging in (again) as needed."""
        for _attempt in (0, 1):
            if not self._logged_in:
                async with self._login_lock:
                    if not self._logged_in:
                        await self.async_login()
            text = await self._attempt_request(path, data)
            if text is not None:
                return text
            self._logged_in = False
        raise NetgearAuthError("Session rejected after re-login")

    async def async_get_info(self) -> SwitchInfo:
        """Return the switch's name, model and firmware version."""
        html = await self._request("/base/system/management/sysInfo.html")
        rows = _rows(html)
        versions = _row_under_header(rows, "Model Name")
        return SwitchInfo(
            name=_input_value(html, "sysName"),
            model=versions.get("Model Name", ""),
            firmware=versions.get("Software Version", ""),
            sys_object_id=_labeled_value(rows, "System Object ID").lstrip("."),
        )

    async def _async_consumed_power(self) -> float | None:
        """Return the switch-wide PoE draw, e.g. "36.7 Watt" -> 36.7."""
        try:
            html = await self._request("/base/poe/poe_cfg.html")
        except NetgearError:
            _LOGGER.debug("Could not read PoE consumption", exc_info=True)
            return None
        text = _labeled_value(_rows(html), "Consumed Power")
        match = re.search(r"[\d.]+", text)
        return float(match.group(0)) if match else None

    async def _async_fetch_port_names(self, retries: int = 0) -> dict[int, str]:
        """Return {port: description} from the port config page.

        Entity names are set from the first poll, so the initial fetch retries
        to ride out a transient switch error. Retries cover only failures: a
        switch where nothing is named answers {} on the first attempt rather
        than retrying its way to the same result.
        """
        last_exc: NetgearError | None = None
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(1.5)
            try:
                html = await self._request("/base/system/port/port_cfg.html")
            except NetgearError as err:
                last_exc = err
                continue
            names = _parse_port_names(html)
            if names is not None:
                return names
            last_exc = NetgearError("No port rows in port_cfg response")
        raise last_exc or NetgearError("Could not fetch port names")

    async def _async_refresh_port_names(self) -> None:
        """Refresh the cached port names, tolerating a transient failure.

        Names rarely change: they are fetched on the first poll (with retries,
        so entities come up named) and occasionally thereafter to pick up a
        rename without a reload.
        """
        if not self.web_port_names_enabled:
            return
        if self._port_names and self._poll_count % 20:
            return
        initial = not self._port_names
        try:
            names = await self._async_fetch_port_names(retries=3 if initial else 0)
        except NetgearError:
            # Keep whatever is cached; only a failure lands here, so an empty
            # result below is the switch's answer rather than a lost page.
            if initial:
                _LOGGER.warning(
                    "Could not fetch port names from %s; ports will be "
                    "unnamed until the next refresh",
                    self.host,
                )
            return
        self._port_names = names

    def _to_poe_port(self, row: dict[str, str]) -> PoePort | None:
        """Map one row of the PoE table to a port, or None if it isn't one."""
        match = _PORT_RE.match(row["Port"])
        if match is None:
            return None
        port = int(match.group(1))
        status = row.get("Status", "").lower()
        return PoePort(
            port=port,
            admin_enabled=row.get("Admin Mode") == "Enable",
            detection_status=_DETECTION_STATUS.get(status, status or "unknown"),
            power_watts=_float_or(row.get("Output Power (Watt)", "")),
            alias=self._port_names.get(port, ""),
            raw=dict(row),
        )

    async def async_get_data(self) -> PoeData:
        """Fetch PoE state for all ports."""
        html = await self._request("/base/poe/poe_port_cfg.html")
        rows = _port_rows(html, ("Port", "Admin Mode", "Status"))
        if not rows:
            raise NetgearError("No PoE ports in poe_port_cfg response")

        await self._async_refresh_port_names()
        self._poll_count += 1

        data = PoeData()
        for row in rows:
            if (port_data := self._to_poe_port(row)) is not None:
                data.ports[port_data.port] = port_data
        data.consumption_watts = await self._async_consumed_power()
        return data

    async def _async_post_form(
        self, path: str, port: int, fields: dict[str, str]
    ) -> None:
        """Post a per-port form and surface any error the page reports.

        Only the fields common to every form live here. The switch answers
        400 to a body carrying a field the target page's form doesn't define
        (e.g. "cncel" on the PoE page), so each caller adds exactly its own.
        """
        body = {
            "unit_no": "1",
            "java_port": "",
            "inputBox_interface1": "",
            "inputBox_interface2": "",
            "selectedPorts": f"g{port};",
            "multiple_ports": "0",
            "submt": _SUBMIT,
            "err_flag": "0",
            "err_msg": "",
            **fields,
        }
        html = await self._request(path, data=body)
        if _input_value(html, "err_flag") == "1":
            reason = _input_value(html, "err_msg") or "unknown error"
            raise NetgearError(f"Switch rejected the change: {reason}")

    async def async_set_port_enabled(self, port: int, enabled: bool) -> None:
        """Enable or disable PoE on a port, preserving its other settings."""
        await self._async_post_form(
            "/base/poe/poe_port_cfg.html",
            port,
            {
                "poeAdminMode": "Enable" if enabled else "Disable",
                # "Blank" means "leave this setting alone".
                "poePriority": "Blank",
                "poeDetectionMode": "Blank",
                "poePowerLimitType": "Blank",
                "poePowerLimit": "",
                "poe_timer_ctrl_list": "0",
                "refrsh": "",
                "click_sched": "",
                "sched_id": "",
            },
        )

    async def async_set_port_name(self, port: int, name: str) -> None:
        """Set the port's description (its name in HA and the SNMP ifAlias)."""
        await self._async_post_form(
            "/base/system/port/port_cfg_rw.html",
            port,
            {
                "portDesc": name,
                # "None" means "leave this setting alone"; an empty frameSize
                # likewise keeps the configured MTU.
                "adminMode": "None",
                "physicalMode": "None",
                "linkTrap": "None",
                "frameSize": "",
                "cncel": "",
            },
        )
        if name:
            self._port_names[port] = name
        else:
            self._port_names.pop(port, None)

    async def async_power_cycle_port(self, port: int) -> None:
        """Power cycle a port; this UI has no native PoE reset."""
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
        """Trap registration is not implemented for this UI."""
        raise NetgearError(
            "SNMP trap registration is not supported on this switch model"
        )

    async def async_close(self) -> None:
        """Close the HTTP session if we own it."""
        self._logged_in = False
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
