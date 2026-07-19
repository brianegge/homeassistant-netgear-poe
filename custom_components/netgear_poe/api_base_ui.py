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
* Firmware: two flash slots ("dual image", system/image_status.html). An
  upgrade is a multipart upload to the inactive slot (system/
  http_file_download.html), activation (system/dual_image_cfg.html) and a
  reboot (system/sys_reset.html — NOT reset_cfg.html, which is the
  factory-default form).

Tables are parsed by column *header* rather than by fixed offsets, since a
port with no description renders an empty cell and firmware revisions move
columns around.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from html import unescape
from typing import ClassVar

import aiohttp

from ._upload import _UPLOAD_CHUNK_BYTES, _multipart_upload_body, _ProgressUpload
from .api import (
    DualImageStatus,
    NetgearAuthError,
    NetgearError,
    PoeData,
    PoePort,
    SwitchInfo,
    _insecure_connector,
)

_LOGGER = logging.getLogger(__name__)

# Re-exported for tests that import these from this module by their old home.
__all__ = ["_UPLOAD_CHUNK_BYTES", "_ProgressUpload", "_multipart_upload_body"]


_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_PORT_RE = re.compile(r"^g(\d+)$")
_LOGIN_FORM_RE = re.compile(r'name="pwd"', re.I)

# The value the UI's JS writes into the hidden "submt" field (0x10).
_SUBMIT = "16"
# Shown when the switch sets err_flag=1 without giving an err_msg.
_UNKNOWN_ERROR = "unknown error"
_POWER_CYCLE_OFF_SECONDS = 3

# Firmware lives in two flash slots ("dual image"); the switch boots whichever
# is marked next-active. An upgrade goes to the inactive slot so the running
# image survives as a rollback.
_IMAGE_STATUS_PATH = "/base/system/image_status.html"
_DUAL_IMAGE_PATH = "/base/system/dual_image_cfg.html"
_FIRMWARE_UPLOAD_PATH = "/base/system/http_file_download.html"
# "Device Reboot". NOT /base/system/reset_cfg.html — that near-identical form
# is "Factory Default" and wipes the switch's configuration.
_REBOOT_PATH = "/base/system/sys_reset.html"
# Logout: POST the session token here, as the UI's logout button does. Shared
# by the classic and cheetah UIs; frees the session slot immediately.
_LOGOUT_PATH = "/base/status.html"

# The switch writes flash while the upload POST is in flight.
_FIRMWARE_UPLOAD_TIMEOUT = 600
# A reboot takes ~45 s end to end; poll well past that before giving up.
_REBOOT_POLL_SECONDS = 5
_REBOOT_POLL_ATTEMPTS = 60
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


def _csrf_token(html: str) -> str:
    """The CSRF token that cheetah firmware >= 1.0.0.44 stamps on every form.

    Older firmware has none, so this returns "". The token must be echoed on
    login (or the session is granted but every later request is bounced to the
    login page) and on every state-changing POST. On the form pages it lives in
    the sibling "a0" applet form, which _cheetah_form scopes out, so writes have
    to add it back explicitly.
    """
    return _input_value(html, "CSRFToken")


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


def _parse_image_status(html: str) -> DualImageStatus:
    """Parse the Dual Image Status table."""
    row = _row_under_header(_rows(html), "Unit")
    current = row.get("Current-active", "").lower()
    if current not in ("image1", "image2"):
        raise NetgearError("Could not read the switch's dual-image status")
    return DualImageStatus(
        versions={
            "image1": row.get("Image1 Ver", ""),
            "image2": row.get("Image2 Ver", ""),
        },
        current_active=current,
        next_active=row.get("Next-active", "").lower(),
    )


def _image_description(html: str, slot: str) -> str:
    """Return a slot's description from the image-status page.

    Rendered as a one-cell label row ("Image1 Description") with the value in
    the row beneath it.
    """
    rows = _rows(html)
    label = f"{slot.capitalize()} Description"
    for index, cells in enumerate(rows[:-1]):
        if label in cells:
            below = rows[index + 1]
            return below[0] if below else ""
    return ""


class NetgearBaseUiApi:
    """Async PoE client for the classic /base/ web UI. Mirrors NetgearPoeApi."""

    # This backend can flash firmware (see async_install_firmware).
    supports_firmware_install = True

    def __init__(
        self,
        host: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
        use_https: bool = False,
    ) -> None:
        self.host = host
        self._password = password
        self._session = session
        self._owns_session = session is None
        # HTTPS-configured switches redirect HTTP to it; detection records the
        # scheme so every request uses the right one.
        self.use_https = use_https
        self._scheme = "https" if use_https else "http"
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self._port_names: dict[int, str] = {}
        # Distinct from `_port_names` being non-empty: a switch with no
        # descriptions is loaded and empty, not unloaded.
        self._port_names_loaded = False
        self._poll_count = 0
        # None until the first poll settles it: some models (the non-PoE
        # GS108Tv2) serve an empty PoE page. An empty page is treated as
        # "no PoE ports" only before any have been seen; once a switch has
        # shown ports, a later empty page is a real error, not a model change.
        self._has_poe: bool | None = None
        # Fetch port names from port_cfg.html; disabled when SNMP (ifAlias) is
        # the name source, to avoid an extra page fetch per refresh.
        self.web_port_names_enabled = True

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=15),
                # HTTPS switches present a self-signed certificate.
                connector=_insecure_connector() if self.use_https else None,
            )
        return self._session

    def _url(self, path: str) -> str:
        return f"{self._scheme}://{self.host}{path}"

    # The login form's location and its exact field set. Subclasses for
    # later firmware (the S350 "cheetah" UI) override these: the switch
    # rejects a body carrying fields the form doesn't define.
    _login_path = "/base/main_login.html"
    _login_extra_fields: ClassVar[dict[str, str]] = {"login.x": "0", "login.y": "0"}

    async def async_login(self) -> None:
        """Authenticate and store the SID session cookie."""
        self._logged_in = False
        session = self._get_session()
        # Fetch the login form first: cheetah firmware >= 1.0.0.44 stamps a
        # CSRFToken on it that must be echoed, or the switch hands out a SID and
        # then bounces every subsequent request to the login page. Older
        # firmware has no token, so this is a harmless extra GET.
        data = {
            "pwd": self._password,
            **self._login_extra_fields,
            "err_flag": "0",
            "err_msg": "",
        }
        try:
            async with session.get(
                self._url(self._login_path),
                headers={"Referer": self._url("/")},
            ) as resp:
                resp.raise_for_status()
                token = _csrf_token(await resp.text(encoding=_ENCODING))
            if token:
                data["CSRFToken"] = token
            async with session.post(
                self._url(self._login_path),
                data=data,
                headers={"Referer": self._url(self._login_path)},
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
        # Referer on every request: a browser always carries one, and the
        # newer cheetah firmware answers 403 to any request without it. A POST
        # to an EmWeb action ("page.html/a1") is refered from the page itself,
        # so drop the trailing "/aN"; base-UI POSTs have none and are
        # unaffected.
        post_referer = re.sub(r"/a\d+$", "", url)
        request = (
            session.get(url, headers={"Referer": self._url("/base/web_main.html")})
            if data is None
            else session.post(url, data=data, headers={"Referer": post_referer})
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

        Whether names have been loaded is tracked separately from whether any
        exist: on a switch where nothing is named, {} is the real answer, and
        keying the throttle off the dict itself would re-run the initial fetch
        (with its retries) on every single poll.
        """
        if not self.web_port_names_enabled:
            return
        if self._port_names_loaded and self._poll_count % 20:
            return
        initial = not self._port_names_loaded
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
        self._port_names_loaded = True

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
        """Fetch PoE state for all ports.

        A non-PoE model (e.g. the GS108Tv2) serves an empty PoE page; that is a
        valid switch with no PoE, returned as empty data so it can still carry
        a firmware-update entity. Once a switch has shown ports, though, an
        empty page is a genuine failure and is raised.
        """
        html = await self._request("/base/poe/poe_port_cfg.html")
        rows = _port_rows(html, ("Port", "Admin Mode", "Status"))
        if not rows:
            if self._has_poe:
                raise NetgearError("No PoE ports in poe_port_cfg response")
            self._has_poe = False
            return PoeData()
        self._has_poe = True

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
            reason = _input_value(html, "err_msg") or _UNKNOWN_ERROR
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

    async def async_get_image_status(self) -> DualImageStatus:
        """Return the dual-image (firmware slot) status."""
        return _parse_image_status(await self._request(_IMAGE_STATUS_PATH))

    async def _async_upload_firmware(
        self,
        slot: str,
        image: bytes,
        filename: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Upload a firmware image to a slot via the HTTP File Download form.

        Posted directly rather than through _request: the body is multipart
        (a browser file upload), and the switch flashes the image while the
        POST is in flight, so it needs its own generous timeout. Because the
        switch reads the body as it writes flash, streaming it in chunks makes
        the progress bar track the real transfer (20..60 here).
        """
        # Field order mirrors the form; a field it doesn't define answers 400.
        # The None marks where the file part goes.
        fields: list[tuple[str, str | None]] = [
            ("file_type", "code"),
            ("localfilename", slot),
            (".filename_handle", None),
            ("download_status", ""),
            ("submt", _SUBMIT),
            ("cncel", ""),
            ("err_flag", "0"),
            ("err_msg", ""),
        ]
        body, content_type = _multipart_upload_body(fields, filename, image)
        # The switch flashes as it reads, so the upload spans 20..60 here.
        payload = _ProgressUpload(body, content_type, progress, 20, 40)

        url = self._url(_FIRMWARE_UPLOAD_PATH)
        try:
            async with self._get_session().post(
                url,
                data=payload,
                headers={"Referer": url},
                timeout=aiohttp.ClientTimeout(total=_FIRMWARE_UPLOAD_TIMEOUT),
            ) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding=_ENCODING)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Firmware upload failed: {err}") from err

        if _LOGIN_FORM_RE.search(text):
            raise NetgearAuthError("Session expired during firmware upload")
        if _input_value(text, "err_flag") == "1":
            reason = _input_value(text, "err_msg") or _UNKNOWN_ERROR
            raise NetgearError(f"Switch rejected the firmware: {reason}")
        status = _input_value(text, "download_status")
        if "success" not in status.lower():
            raise NetgearError(
                f"Firmware upload did not complete: {status or 'no status reported'}"
            )

    async def _async_activate_image(self, slot: str) -> None:
        """Mark a slot as the image to boot from (next-active)."""
        # The form echoes the slot's description back; read the current one so
        # the post can't clobber it (update_flag=0 leaves it alone anyway).
        status_html = await self._request(_IMAGE_STATUS_PATH)
        html = await self._request(
            _DUAL_IMAGE_PATH,
            data={
                "image_name": slot.capitalize(),  # option values: Image1/Image2
                "current_active": _parse_image_status(status_html).current_active,
                "image_descrip": _image_description(status_html, slot),
                "act_img": "checkbox",
                "delete": "",
                "refrsh": "",
                "clear": "",
                "submt": _SUBMIT,
                "activate_flag": "1",
                "update_flag": "0",
                "err_flag": "0",
                "err_msg": "",
                "delete_flag": "0",
            },
        )
        if _input_value(html, "err_flag") == "1":
            reason = _input_value(html, "err_msg") or _UNKNOWN_ERROR
            raise NetgearError(f"Could not activate {slot}: {reason}")

    async def async_reboot(self) -> None:
        """Reboot the switch (drops PoE and the switch's uplink for ~a minute).

        A cold reboot from the web UI, the recovery for a wedged PoE
        controller or a hung SNMP agent. Establishes a session first so the
        reboot form post is authenticated, then issues it. Inherited by the
        cheetah subclass, whose own _async_reboot override runs instead.
        """
        if not self._logged_in:
            async with self._login_lock:
                if not self._logged_in:
                    await self.async_login()
        await self._async_reboot()

    async def _async_reboot(self) -> None:
        """Reboot the switch via the Device Reboot form.

        The switch usually drops the connection as it goes down, so a
        connection error here means the reboot took, not that it failed.
        """
        url = self._url(_REBOOT_PATH)
        try:
            async with self._get_session().post(
                url,
                data={"CBox_2": "0", "submt": _SUBMIT, "cncel": ""},
                headers={"Referer": url},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding=_ENCODING)
        except (aiohttp.ClientError, TimeoutError):
            # Losing the connection IS the reboot; anything else is answered
            # below by a switch that is still very much awake.
            self._logged_in = False
            return
        # A complete reply means the reboot was refused. Say so here rather
        # than leaving the caller to sit out the whole reboot timeout.
        self._logged_in = False
        if _LOGIN_FORM_RE.search(text):
            raise NetgearAuthError("Session expired before the reboot request")
        if _input_value(text, "err_flag") == "1":
            reason = _input_value(text, "err_msg") or _UNKNOWN_ERROR
            raise NetgearError(f"Switch refused to reboot: {reason}")

    async def _async_wait_for_firmware(
        self, version: str, progress: Callable[[int], None] | None = None
    ) -> None:
        """Poll until the rebooted switch reports `version`.

        Early polls may still reach the old firmware (the switch keeps
        answering for a few seconds before it actually goes down), so a
        mismatch keeps waiting; only the deadline decides. Progress walks
        80 -> 95 as the retry budget is spent — each poll is a real step,
        so the bar reports measurement, not animation.
        """
        observed = ""
        for attempt in range(1, _REBOOT_POLL_ATTEMPTS + 1):
            await asyncio.sleep(_REBOOT_POLL_SECONDS)
            if progress is not None:
                progress(80 + (attempt * 15) // _REBOOT_POLL_ATTEMPTS)
            try:
                observed = (await self.async_get_info()).firmware
            except NetgearError:
                continue
            if observed == version:
                return
        raise NetgearError(
            f"Switch did not come back on firmware {version} within "
            f"{_REBOOT_POLL_ATTEMPTS * _REBOOT_POLL_SECONDS} s"
            + (f" (last reported {observed})" if observed else "")
        )

    async def async_install_firmware(
        self,
        image: bytes,
        version: str,
        filename: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Install a firmware image: upload, activate, reboot, verify.

        The image always goes to the INACTIVE slot, so the running firmware
        stays flashed as a rollback (activate the other slot and reboot to
        revert). The final reboot drops PoE — and the switch's own link —
        for around a minute. `version` must match what the image reports
        after flashing; it is how the upload and the reboot are verified.
        """

        def report(percent: int) -> None:
            if progress is not None:
                progress(percent)

        status = await self.async_get_image_status()
        target = status.inactive
        _LOGGER.info(
            "Uploading firmware %s to %s slot %s (running %s from %s)",
            version,
            self.host,
            target,
            status.versions.get(status.current_active, "?"),
            status.current_active,
        )
        report(20)
        await self._async_upload_firmware(target, image, filename, progress)

        staged = await self.async_get_image_status()
        if staged.versions.get(target) != version:
            raise NetgearError(
                f"Upload finished but slot {target} reports "
                f"{staged.versions.get(target) or 'nothing'}, expected {version}"
            )
        report(60)

        await self._async_activate_image(target)
        if (await self.async_get_image_status()).next_active != target:
            raise NetgearError(f"Could not make {target} the boot image")
        report(70)

        _LOGGER.info(
            "Rebooting %s into %s (%s); PoE will drop briefly",
            self.host,
            target,
            version,
        )
        await self._async_reboot()
        report(80)
        await self._async_wait_for_firmware(version, progress)
        report(100)
        _LOGGER.info("%s is now running firmware %s", self.host, version)

    async def async_ensure_trap_destination(self, dest_ip: str, community: str) -> None:
        """Trap registration is not implemented for this UI."""
        raise NetgearError(
            "SNMP trap registration is not supported on this switch model"
        )

    async def async_logout(self) -> None:
        """End the web session so its slot is freed immediately.

        These switches allow very few concurrent HTTP sessions — as few as
        four — with a long idle timeout, so a leaked session can lock the UI
        (and this integration) out for up to an hour. This posts the session
        token back to /base/status.html, the exact request the UI's logout
        button makes; without it, every reload/restart would strand a slot.
        """
        if not self._logged_in:
            return
        try:
            page = await self._attempt_request(_LOGOUT_PATH, None)
            token = _input_value(page, "sessionID") if page else ""
            if token:
                await self._attempt_request(_LOGOUT_PATH, {"sessionID": token})
        except NetgearError:
            # A failed logout only means the slot waits out its idle timeout.
            _LOGGER.debug("Logout request failed on %s", self.host, exc_info=True)
        finally:
            self._logged_in = False

    async def async_close(self) -> None:
        """Log out and close the HTTP session if we own it."""
        await self.async_logout()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


# Cheetah tables render each cell as an EmWeb hidden input whose NAME encodes
# the row: "1.<index>.<count>.v_1_2_<col>", e.g. 1.5.24.v_1_2_2 is column 2 of
# the sixth PoE port. The value is both the input's VALUE and the cell text.
_CHEETAH_CELL_RE = re.compile(
    r'NAME=1\.(\d+)\.\d+\.v_\d+_\d+_(\d+)\s+VALUE="([^"]*)"', re.I
)
# Its column order is fixed by firmware, keyed off the g-label always in col 1.
_CHEETAH_POE_COLS = {1: "port", 2: "admin", 15: "power", 17: "status"}
_CHEETAH_PORT_COLS = {1: "port", 2: "alias"}
_CHEETAH_ADMIN_COL = 2
# The <td aid='1_<r>_1'> row on sysInfo.html: model, boot, then software.
_CHEETAH_AID_RE = re.compile(r"aid=['\"]1_(\d+)_1['\"][^>]*>([^<]*)<", re.I)

# A posted cell, capturing its full name, port index, cell count and column.
_CHEETAH_POST_CELL_RE = re.compile(
    r"NAME=(1\.(\d+)\.(\d+)\.v_\d+_\d+_(\d+))\s+VALUE=\"([^\"]*)\"", re.I
)
# Hidden control fields the form carries alongside the table.
_CHEETAH_CONTROL_FIELDS = (
    "submit_target",
    "err_flag",
    "err_msg",
    "clazz_information",
)
# xui_operation_submit: the Apply button sets submit_flag to this before
# posting. The form's own default is 0 ("reload"), which the switch renders
# but never applies — so an edit only takes effect when we send 8.
_CHEETAH_SUBMIT_OP = "8"

# Firmware pages (EmWeb routes at the site root). "File Download" is Netgear's
# name for a download *to* the switch.
_CHEETAH_UPLOAD_PATH = "/http_file_download.html"
_CHEETAH_IMAGE_STATUS_PATH = "/dualImageStatus.html"
_CHEETAH_DUAL_IMAGE_PATH = "/dualImageConfiguration.html"
_CHEETAH_REBOOT_PATH = "/deviceReboot.html"
# Upload form fields. These values were confirmed against a packet capture of
# the switch's own web UI performing a successful firmware upload — the switch's
# error messages are misleading, so guessing from the page markup was not
# enough. The winning request posts, alongside the file:
#   v_1_10_1  = "Code"   File Type, as the LABEL (not the enum index)
#   v_1_2_2   = imageN   Image Name combo (the selected slot)
#   v_1_9_1   = imageN   the REAL destination — "Local HTTP File Name"; the
#                        combo above is only a UI helper that copies into this
#   v_1_3_4   = filename the uploaded file's name, echoed into a hidden field
#   v_1_9_2   = "1"      "HTTP Download Start" — the flag that begins the flash
#   v_1_1_3   = "HTTP"   Transfer Mode
#   submit_flag = "8"    apply (forced by the replay)
_CHEETAH_FILE_TYPE_FIELD = "v_1_10_1"
_CHEETAH_FILE_TYPE_CODE = "Code"
_CHEETAH_UPLOAD_SLOT_FIELD = "v_1_2_2"
_CHEETAH_DEST_SLOT_FIELD = "v_1_9_1"
_CHEETAH_FILE_NAME_FIELD = "v_1_3_4"
_CHEETAH_UPLOAD_FILE_FIELD = ".v_1_3_1_handle"
# The switch renders every write-only field with an empty VALUE and relies on
# its own JS to fill them back in before submitting: xui_load.js's
# xuiLoadElementValuesFromJS() copies xeData.xeleValue_<xid> into any input the
# server left blank. Replaying the rendered form therefore posts an empty
# "HTTP Download Start", and the switch accepts the whole 26 MB upload, reports
# no error, and never writes flash.
#
# Only these singleton fields are filled in, deliberately: the same rule
# applied form-wide would post Port Reset="Reset" for every port of the PoE
# table (its cells are write-only too), power-cycling every camera on the
# switch. A browser gets away with it because widget construction runs after
# the fill and overwrites those cells; replaying the raw form does not.
_CHEETAH_UPLOAD_DEFAULTS = {
    # Transfer Mode. A hidden enum keeps its label rather than the index a
    # combo would post.
    "v_1_1_3": "HTTP",
    # "HTTP Download Start" — the flag that actually starts the flash.
    "v_1_9_2": "1",
}
# "Transfer In Progress" (L7_BOOL: "In progress" / " not in progress").
_CHEETAH_TRANSFER_FIELD = "v_1_3_2"
_CHEETAH_IN_PROGRESS = "In progress"
# Activate form (confirmed against a packet capture of a working browser
# activation). Ticking the "Activate Image" checkbox is not enough — the switch
# reads a hidden companion, v_4_3_2 ("Active Image") = "TRUE", and the browser
# also fills the description (v_4_2_1) with the version. Setting only the
# checkbox posts cleanly and does nothing (next-active stays unchanged).
_CHEETAH_ACTIVATE_SLOT_FIELD = "v_4_1_1"
_CHEETAH_ACTIVATE_FIELD = "v_4_3_1"
_CHEETAH_ACTIVE_IMAGE_FIELD = "v_4_3_2"
_CHEETAH_ACTIVE_IMAGE_ON = "TRUE"
_CHEETAH_ACTIVATE_DESC_FIELD = "v_4_2_1"
# Reboot form (same capture): the real trigger is v_1_1_2 ("Reset Unit") = "1";
# the visible confirm checkbox (v_1_2_1) alone does not reboot.
_CHEETAH_REBOOT_CONFIRM_FIELD = "v_1_2_1"
_CHEETAH_REBOOT_UNIT_FIELD = "v_1_1_2"
_CHEETAH_REBOOT_UNIT_ON = "1"
_CHEETAH_CHECKED = "Enable"
# The switch buffers the image in seconds but then writes flash in the
# background for tens of minutes (a 3 MB image takes 5+ min on this family, so
# a 26 MB one runs far longer). Poll the transfer flag, not the version.
_CHEETAH_FLASH_POLL_SECONDS = 15
_CHEETAH_FLASH_POLL_ATTEMPTS = 240  # up to an hour
# Polls to allow before concluding the switch never began writing: the flash
# starts within seconds, so this only avoids a race on the very first read.
_CHEETAH_FLASH_START_GRACE = 4


def _cheetah_field_value(html: str, name: str) -> str:
    """Return a cheetah form field's VALUE (its NAME may be unquoted)."""
    match = re.search(
        rf'NAME=["\']?{re.escape(name)}["\']?[^>]*VALUE="([^"]*)"', html, re.I
    )
    return unescape(match.group(1)) if match else ""


def _cheetah_form(html: str, action: str) -> str:
    """Return just the form whose ACTION ends with `action`.

    These pages carry more than one form — a small "a0" applet form sits
    above the real "a1" one. Scoping matters: the switch quietly drops a
    submission that carries a field the target form doesn't define, so a
    body scraped from the whole page uploads cleanly and flashes nothing.
    """
    match = re.search(rf'<FORM[^>]*ACTION="[^"]*{re.escape(action)}"', html, re.I)
    if match is None:
        raise NetgearError(f"Could not find the {action} form on the switch")
    end = html.find("</FORM>", match.end())
    return html[match.end() : end if end > 0 else len(html)]


def _cheetah_replay_ordered(
    html: str, changes: dict[str, str]
) -> list[tuple[str, str | None]]:
    """Replay a cheetah form's posted fields *in document order*.

    Pass only the target form's markup (see _cheetah_form). The EmWeb submit
    posts the whole form back with only the edited controls altered, so
    replaying verbatim is what keeps one change from disturbing anything
    else, and submit_flag is forced to "apply". Order is preserved because
    the multipart parser is positional. The file input is yielded as a
    (name, None) placeholder for the caller to fill.
    """
    fields: list[tuple[str, str | None]] = []
    for match in re.finditer(
        r'NAME=("?)([.\w]+)\1[^>]*(?:TYPE=(file)\b[^>]*)?VALUE="([^"]*)"', html, re.I
    ):
        name, value = match.group(2), unescape(match.group(4))
        if name.endswith("_handle"):
            fields.append((name, None))  # the file goes exactly here
            continue
        if name == "submit_flag":
            value = _CHEETAH_SUBMIT_OP
        elif name in changes:
            value = changes[name]
        fields.append((name, value))
    have = {n for n, _ in fields}
    for name, value in changes.items():
        if name not in have:
            fields.append((name, value))
    if "submit_flag" not in have:
        fields.append(("submit_flag", _CHEETAH_SUBMIT_OP))
    return fields


def _cheetah_replay(html: str, changes: dict[str, str]) -> dict[str, str]:
    """Replay a cheetah form as a plain body (no file part)."""
    return {
        name: value
        for name, value in _cheetah_replay_ordered(html, changes)
        if value is not None
    }


def _cheetah_rows(html: str, columns: dict[int, str]) -> list[dict[str, str]]:
    """Group cheetah table cells into per-port rows keyed by column name."""
    rows: dict[int, dict[str, str]] = {}
    for index, col, value in _CHEETAH_CELL_RE.findall(html):
        name = columns.get(int(col))
        if name is not None:
            rows.setdefault(int(index), {})[name] = unescape(value).strip()
    # Only rows that carry a real "g<n>" port label are data rows.
    return [
        row
        for _index, row in sorted(rows.items())
        if _PORT_RE.match(row.get("port", ""))
    ]


def _cheetah_poe_write_body(
    html: str, port: int, admin_value: str
) -> dict[str, str] | None:
    """Build the PoE-form POST that flips one port's admin mode.

    The EmWeb form echoes the whole table back; the browser posts every
    hidden cell at its current value and overwrites only the edited one.
    Mirroring that means no other port can change — every field but the
    target rides along verbatim. Returns None if the port isn't present.
    """
    target = port - 1  # rows are 0-based; g1 is index 0
    count: str | None = None
    found = False
    body: dict[str, str] = {}
    for name, index, cells, col, value in _CHEETAH_POST_CELL_RE.findall(html):
        if int(index) == target and int(col) == _CHEETAH_ADMIN_COL:
            value = admin_value
            count = cells
            found = True
        body[name] = value
    if not found or count is None:
        return None
    # Check the target row's select box, as the browser does on an edit.
    body[f"1.{target}.{count}.gecb_1_{_CHEETAH_ADMIN_COL}"] = "on"
    for field in _CHEETAH_CONTROL_FIELDS:
        body[field] = _input_value(html, field)
    # Apply, not reload — see _CHEETAH_SUBMIT_OP.
    body["submit_flag"] = _CHEETAH_SUBMIT_OP
    # Firmware >= 1.0.0.44 rejects a state-changing POST without the page's
    # CSRF token; it sits outside the table, so add it explicitly.
    token = _csrf_token(html)
    if token:
        body["CSRFToken"] = token
    return body


class NetgearCheetahApi(NetgearBaseUiApi):
    """Client for the S350-series ("cheetah") firmware, e.g. the GS324TP.

    A hardened evolution of the same FASTPATH web UI: the login form moved
    to /base/cheetah_login.html (same pwd/err_flag/submt convention, plus a
    Refererless-request 403 the base class now always satisfies), and the
    per-feature pages are compiled-in EmWeb routes at the site root rather
    than files under /base/. Repeated failed logins trip a temporary
    lockout, so a wrong password here stays wrong for a while even after
    it is corrected.

    The tables are laid out differently too: each cell is an EmWeb input
    named "1.<index>.<count>.v_..._<col>" rather than a header-aligned <td>,
    so the read helpers here parse by that scheme instead of by column
    header. PoE control posts the whole table back with one port's admin
    cell changed and submit_flag=8 ("apply", vs the form's 0 = "reload");
    see _cheetah_poe_write_body. The ifAlias write is a separate form and is
    not implemented yet.
    """

    _login_path = "/base/cheetah_login.html"
    # Its form defines exactly pwd/err_flag/err_msg/submt — no login.x/y.
    _login_extra_fields: ClassVar[dict[str, str]] = {"submt": _SUBMIT}

    supports_firmware_install = True

    async def async_get_info(self) -> SwitchInfo:
        """Return the switch's name, model and firmware version."""
        html = await self._request("/base/system/management/sysInfo.html")
        # The versions row renders as <td aid='1_2_1'>model</td>
        # <td aid='1_3_1'>boot</td> <td aid='1_4_1'>software</td>.
        aids = {int(r): text.strip() for r, text in _CHEETAH_AID_RE.findall(html)}
        # The System Object OID is the one Netgear-enterprise OID on the page.
        oid = re.search(r"(1\.3\.6\.1\.4\.1\.4526[\d.]*\d)", html)
        return SwitchInfo(
            name=_input_value(html, "sysName"),
            model=aids.get(2, ""),
            firmware=aids.get(4, ""),
            sys_object_id=(oid.group(1) if oid else ""),
        )

    async def _async_consumed_power(self) -> float | None:
        """Return the switch-wide PoE draw from the PoE config page."""
        try:
            html = await self._request("/poeConfiguration.html")
        except NetgearError:
            _LOGGER.debug("Could not read PoE consumption", exc_info=True)
            return None
        match = re.search(
            r'basePoeGlobalConfig_MainPseConsumptionPower[^"]*"([\d.]+)"', html
        )
        if match is None:
            match = re.search(
                r'VALUE="([\d.]+)"[^<]*</TD>\s*<!--\s*'
                r"basePoeGlobalConfig_MainPseConsumptionPower",
                html,
            )
        return float(match.group(1)) if match else None

    async def _async_fetch_port_names(self, retries: int = 0) -> dict[int, str]:
        """Return {port: ifAlias} from the ports configuration page."""
        last_exc: NetgearError | None = None
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(1.5)
            try:
                html = await self._request("/portsConfiguration.html")
            except NetgearError as err:
                last_exc = err
                continue
            names: dict[int, str] = {}
            for row in _cheetah_rows(html, _CHEETAH_PORT_COLS):
                match = _PORT_RE.match(row["port"])
                alias = row.get("alias", "").strip()
                if match and alias:
                    names[int(match.group(1))] = alias
            return names
        raise last_exc or NetgearError("Could not fetch port names")

    async def async_get_data(self) -> PoeData:
        """Fetch PoE state for all ports."""
        html = await self._request("/poeInterfaceConfiguration.html")
        rows = _cheetah_rows(html, _CHEETAH_POE_COLS)
        if not rows:
            raise NetgearError("No PoE ports in poeInterfaceConfiguration response")

        await self._async_refresh_port_names()
        self._poll_count += 1

        data = PoeData()
        for row in rows:
            match = _PORT_RE.match(row["port"])
            if match is None:
                continue
            port = int(match.group(1))
            status = row.get("status", "").lower()
            data.ports[port] = PoePort(
                port=port,
                admin_enabled=row.get("admin") == "Enable",
                detection_status=_DETECTION_STATUS.get(status, status or "unknown"),
                power_watts=_float_or(row.get("power", "")),
                alias=self._port_names.get(port, ""),
                raw=dict(row),
            )
        data.consumption_watts = await self._async_consumed_power()
        return data

    async def async_set_port_enabled(self, port: int, enabled: bool) -> None:
        """Enable or disable PoE on a port, preserving every other setting.

        Posts the whole PoE table back with just this port's admin mode
        changed (see _cheetah_poe_write_body). async_power_cycle_port is
        inherited and drives this.
        """
        html = await self._request("/poeInterfaceConfiguration.html")
        body = _cheetah_poe_write_body(html, port, "Enable" if enabled else "Disable")
        if body is None:
            raise NetgearError(f"Port g{port} not found on the switch")
        resp = await self._request("/poeInterfaceConfiguration.html/a1", data=body)
        if _input_value(resp, "err_flag") == "1":
            reason = _input_value(resp, "err_msg") or _UNKNOWN_ERROR
            raise NetgearError(f"Switch rejected the change: {reason}")

    async def async_set_port_name(self, port: int, name: str) -> None:
        """Not implemented on this generation yet.

        Port PoE control works; the ifAlias write is a separate EmWeb form
        that still needs its own hardware-tested reverse engineering. SNMP
        supplies port names in the meantime.
        """
        raise NetgearError(
            "Setting port names is not yet supported on S350-series switches"
        )

    async def async_get_image_status(self) -> DualImageStatus:
        """Return the dual-image (firmware slot) status."""
        html = await self._request(_CHEETAH_IMAGE_STATUS_PATH)

        def cell(field: str) -> str:
            match = re.search(
                rf'VALUE="([^"]*)"[^<]*</TD>\s*<!--\s*basesysImage_{field}\b', html
            )
            return match.group(1).strip() if match else ""

        current = cell("sysActiveImageName").lower()
        if current not in ("image1", "image2"):
            raise NetgearError("Could not read the switch's dual-image status")
        return DualImageStatus(
            versions={
                "image1": cell("sysImage1Version"),
                "image2": cell("sysImage2Version"),
            },
            current_active=current,
            next_active=cell("sysActivatedImageName").lower() or current,
        )

    async def _async_transfer_in_progress(self) -> bool:
        """True while the switch is writing an uploaded image to flash."""
        html = await self._request(_CHEETAH_UPLOAD_PATH)
        value = _cheetah_field_value(html, _CHEETAH_TRANSFER_FIELD)
        return value.strip().lower() == _CHEETAH_IN_PROGRESS.lower()

    async def _async_upload_firmware(
        self,
        image: bytes,
        filename: str,
        slot: str,
        version: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Upload an image to `slot` and wait for the switch to flash it.

        The POST returns as soon as the switch has buffered the image — for a
        26 MB file that is seconds, while the flash itself then runs in the
        background for tens of minutes. "Transfer In Progress" is the only
        signal that it is still working, so waiting on the version instead
        (or giving up after a few minutes) looks like a silent failure.
        """
        page = await self._request(_CHEETAH_UPLOAD_PATH)
        changes = {
            **_CHEETAH_UPLOAD_DEFAULTS,
            _CHEETAH_FILE_TYPE_FIELD: _CHEETAH_FILE_TYPE_CODE,
            _CHEETAH_UPLOAD_SLOT_FIELD: slot,
            _CHEETAH_DEST_SLOT_FIELD: slot,
            _CHEETAH_FILE_NAME_FIELD: filename,
        }
        token = _csrf_token(page)  # firmware >= 1.0.0.44; empty on older
        if token:
            changes["CSRFToken"] = token
        fields = _cheetah_replay_ordered(_cheetah_form(page, "/a1"), changes)
        body, content_type = _multipart_upload_body(fields, filename, image)
        # The switch buffers fast then flashes: give the upload 20..40 and
        # leave 40..60 for the flash-write poll below.
        payload = _ProgressUpload(body, content_type, progress, 20, 20)

        url = self._url(f"{_CHEETAH_UPLOAD_PATH}/a1")
        try:
            async with self._get_session().post(
                url,
                data=payload,
                headers={"Referer": self._url(_CHEETAH_UPLOAD_PATH)},
                timeout=aiohttp.ClientTimeout(total=_FIRMWARE_UPLOAD_TIMEOUT),
            ) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding=_ENCODING)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Firmware upload failed: {err}") from err
        if _LOGIN_FORM_RE.search(text):
            raise NetgearAuthError("Session expired during firmware upload")
        reason = _cheetah_field_value(text, "err_msg").strip()
        if reason:
            raise NetgearError(f"Switch rejected the firmware: {reason}")

        _LOGGER.info("%s buffered the image; waiting for it to write flash", self.host)
        started = False
        for attempt in range(_CHEETAH_FLASH_POLL_ATTEMPTS):
            await asyncio.sleep(_CHEETAH_FLASH_POLL_SECONDS)
            if progress is not None:
                # 40..60 across the flash window (the upload used 20..40); a
                # real wait, not a spinner, but the switch reports no byte
                # count to scale by, so this tracks elapsed poll attempts.
                progress(40 + min(attempt * 20 // _CHEETAH_FLASH_POLL_ATTEMPTS, 19))
            try:
                if await self._async_transfer_in_progress():
                    started = True
                    continue
                # Not writing: either finished, or it never began.
                if (await self.async_get_image_status()).versions.get(slot) == version:
                    _LOGGER.info("%s finished writing %s", self.host, slot)
                    return
                if started:
                    raise NetgearError(
                        f"Switch stopped writing flash but {slot} does not hold "
                        f"{version}"
                    )
                if attempt >= _CHEETAH_FLASH_START_GRACE:
                    raise NetgearError(
                        "Switch accepted the upload but never started writing "
                        "flash — it did not take the image"
                    )
            except NetgearAuthError:
                raise
            except NetgearError:
                if started:
                    continue  # busy flashing; its web UI can be unresponsive
                raise
        raise NetgearError(
            "Switch was still writing flash after "
            f"{_CHEETAH_FLASH_POLL_ATTEMPTS * _CHEETAH_FLASH_POLL_SECONDS // 60} min"
        )

    async def _async_activate_image(self, slot: str, description: str = "") -> None:
        """Mark a slot as the image to boot from (next-active)."""
        page = await self._request(_CHEETAH_DUAL_IMAGE_PATH)
        changes = {
            _CHEETAH_ACTIVATE_SLOT_FIELD: slot,
            _CHEETAH_ACTIVATE_FIELD: _CHEETAH_CHECKED,
            # The hidden companion the switch actually reads; the checkbox
            # above is only the UI. Without it the POST is a no-op.
            _CHEETAH_ACTIVE_IMAGE_FIELD: _CHEETAH_ACTIVE_IMAGE_ON,
            _CHEETAH_ACTIVATE_DESC_FIELD: description,
        }
        token = _csrf_token(page)  # firmware >= 1.0.0.44; empty on older
        if token:
            changes["CSRFToken"] = token
        body = _cheetah_replay(_cheetah_form(page, "/a1"), changes)
        resp = await self._request(f"{_CHEETAH_DUAL_IMAGE_PATH}/a1", data=body)
        reason = _cheetah_field_value(resp, "err_msg").strip()
        if reason:
            raise NetgearError(f"Could not activate {slot}: {reason}")

    async def _async_reboot(self) -> None:
        """Reboot the switch via the Device Reboot form.

        Preflight (fetching the form, its token, building the body) runs
        outside the try so a failure there propagates — otherwise the caller
        would wait out the whole reboot timeout for a reboot never requested.
        Only the final POST is expected to fail, because the switch drops the
        connection as it goes down; that dropped POST is the success signal.
        """
        page = await self._request(_CHEETAH_REBOOT_PATH)
        changes = {
            _CHEETAH_REBOOT_CONFIRM_FIELD: _CHEETAH_CHECKED,
            # "Reset Unit" — the actual reboot trigger; the confirm checkbox
            # alone does not restart the switch.
            _CHEETAH_REBOOT_UNIT_FIELD: _CHEETAH_REBOOT_UNIT_ON,
        }
        token = _csrf_token(page)  # firmware >= 1.0.0.44; empty on older
        if token:
            changes["CSRFToken"] = token
        body = _cheetah_replay(_cheetah_form(page, "/a1"), changes)
        self._logged_in = False
        try:
            resp = await self._request(f"{_CHEETAH_REBOOT_PATH}/a1", data=body)
        except NetgearError:
            # The switch drops the connection as it goes down: that IS success.
            _LOGGER.debug("Reboot request did not answer (expected)", exc_info=True)
            return
        # A complete reply means the reboot was refused; surface it rather than
        # leaving the caller to sit out the whole reboot timeout.
        reason = _cheetah_field_value(resp, "err_msg").strip()
        if reason:
            raise NetgearError(f"Switch refused to reboot: {reason}")

    async def async_install_firmware(
        self,
        image: bytes,
        version: str,
        filename: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Install a firmware image: upload, activate, reboot, verify.

        The image goes to the INACTIVE slot, so the running firmware stays
        flashed as a rollback. The flash takes tens of minutes before the
        reboot even starts; the caller should stop polling the switch for the
        duration, since it allows very few concurrent sessions.
        """

        def report(percent: int) -> None:
            if progress is not None:
                progress(percent)

        status = await self.async_get_image_status()
        target = status.inactive
        _LOGGER.info(
            "Uploading firmware %s to %s slot %s (running %s from %s)",
            version,
            self.host,
            target,
            status.versions.get(status.current_active, "?"),
            status.current_active,
        )
        report(20)
        await self._async_upload_firmware(image, filename, target, version, progress)

        staged = await self.async_get_image_status()
        if staged.versions.get(target) != version:
            raise NetgearError(
                f"Flash finished but slot {target} reports "
                f"{staged.versions.get(target) or 'nothing'}, expected {version}"
            )
        report(65)

        await self._async_activate_image(target, version)
        if (await self.async_get_image_status()).next_active != target:
            raise NetgearError(f"Could not make {target} the boot image")
        report(70)

        _LOGGER.info("Rebooting %s into %s (%s)", self.host, target, version)
        await self._async_reboot()
        report(80)
        await self._async_wait_for_firmware(version, progress)
        report(100)
        _LOGGER.info("%s is now running firmware %s", self.host, version)
