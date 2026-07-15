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
* Firmware: two flash slots ({ImageUnitList}). An upgrade is a multipart
  upload of Netgear's .ros archive to Maintenance/httpConfigProcess.htm,
  which the switch writes to the inactive slot by itself (the form has no
  slot field); {LoadStatus} reports progress and the outcome. Activation
  sets nextBootImage via {ImageUnitList}, and {Reload} reboots.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from xml.sax.saxutils import escape

import aiohttp
from yarl import URL

from .api import (
    DualImageStatus,
    NetgearAuthError,
    NetgearError,
    NetgearPoeApi,
    PoeData,
    PoePort,
    SwitchInfo,
)
from .api_base_ui import NetgearBaseUiApi

_LOGGER = logging.getLogger(__name__)

# Any of the interchangeable per-generation clients async_detect_api returns.
type NetgearAnyApi = NetgearPoeApi | NetgearLegacyApi | NetgearBaseUiApi

# The per-request path prefix: "csb" followed by hex, e.g. /csb555f027/.
# It is NOT stable — the same switch answers with a different prefix over
# time, and every xui switch on a network answers with the same one at any
# given moment, so it can't be cached across logins (async_login re-reads it).
# Matching only "csbe" + digits, as this once did, works by luck: it needs the
# hex to start with 'e' and carry no a-f after it.
_PREFIX_RE = re.compile(r"/(csb[0-9a-f]+)/", re.I)
_BASE_UI_RE = re.compile(r"/base/main_login\.html", re.I)
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

# Firmware transfer. "File Download" is Netgear's name for a download *to* the
# switch; file_type 8 = Archive (the flashable .ros).
_UPLOAD_PATH = "Maintenance/httpConfigProcess.htm"
_ARCHIVE_FILE_TYPE = "8"
# {LoadStatus} copyStatusType values.
_COPY_IN_PROGRESS = ("1", "2")
_COPY_FAILED = ("3", "4")
_COPY_DONE = "5"
# The switch writes flash while the upload POST is in flight.
_FIRMWARE_UPLOAD_TIMEOUT = 900
_UPLOAD_POLL_SECONDS = 2
# Observed ~110 s on a GS516TP, but a 6.0.1.x first boot can also auto-upgrade
# the PoE controller (Netgear documents ~8 min), so allow well past that.
_REBOOT_POLL_SECONDS = 5
_REBOOT_POLL_ATTEMPTS = 144


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

    # This backend can flash firmware (see async_install_firmware).
    supports_firmware_install = True

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
            raise NetgearError(f"{self.host} did not redirect to a legacy UI prefix")
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

    async def async_get_info(self) -> SwitchInfo:
        """Return the switch's name, model and firmware version."""
        root = await self._request("wcd?{DeviceBasicInfo}")
        info = root.find(".//DeviceBasicInfo")
        if info is None:
            raise NetgearError("No DeviceBasicInfo in response")
        # The firmware element name varies across xui firmware builds.
        firmware = next(
            (
                text.strip()
                for tag in ("firmwareVersion", "softwareVersion", "swVer")
                if (text := info.findtext(tag)) and text.strip()
            ),
            "",
        )
        return SwitchInfo(
            name=info.findtext("deviceName", "").strip(),
            model=info.findtext("deviceDescription", "").strip(),
            firmware=firmware,
            sys_object_id=info.findtext("systemObjectID", "").strip().lstrip("."),
        )

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

    async def _async_set_interface(self, port: int, elements: str) -> None:
        """Post a PoEPSEInterfaceList set for one port and check the status."""
        if_name = await self._async_if_name(port)
        body = (
            "<?xml version='1.0' encoding='utf-8'?>"
            '<DeviceConfiguration set="set">'
            '<PoEPSEInterfaceList action="set" set="set">'
            f"<Interface><interfaceName>{if_name}</interfaceName>"
            f"{elements}</Interface>"
            "</PoEPSEInterfaceList></DeviceConfiguration>"
        )
        root = await self._request("wcd", body=body)
        code = root.findtext(".//ActionStatus/statusCode", "").strip()
        if code not in ("", "0"):
            status = root.findtext(".//ActionStatus/statusString", "").strip()
            raise NetgearError(f"PoE set failed: {status or code}")

    async def async_set_port_enabled(self, port: int, enabled: bool) -> None:
        """Enable or disable PoE on a port."""
        # SNMP TruthValue semantics (pethPsePortAdminEnable): 2 = disable
        await self._async_set_interface(
            port, f"<adminEnable>{1 if enabled else 2}</adminEnable>"
        )

    async def async_set_port_name(self, port: int, name: str) -> None:
        """Set the port's powered-device description (its name in HA)."""
        await self._async_set_interface(
            port, f"<poweredDevice>{escape(name)}</poweredDevice>"
        )

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

    async def async_get_image_status(self) -> DualImageStatus:
        """Return the dual-image (firmware slot) status."""
        root = await self._request("wcd?{ImageUnitList}")
        entry = root.find(".//ImageUnitList/Entry")
        if entry is None:
            raise NetgearError("Could not read the switch's dual-image status")
        text = {child.tag: (child.text or "").strip() for child in entry}
        current = text.get("currentImage", "")
        if current not in ("1", "2"):
            raise NetgearError(f"Switch reported no current image ({current!r})")
        return DualImageStatus(
            versions={
                "image1": text.get("image1Version", ""),
                "image2": text.get("image2Version", ""),
            },
            current_active=f"image{current}",
            next_active=f"image{text.get('nextBootImage', current)}",
        )

    async def _async_load_status(self) -> tuple[str, str, int]:
        """Return the file-transfer state: (status, error message, bytes)."""
        root = await self._request("wcd?{LoadStatus}")
        status = root.find(".//LoadStatus")
        if status is None:
            return "", "", 0
        text = {child.tag: (child.text or "").strip() for child in status}
        try:
            transferred = int(text.get("bytesTransfered", "0"))
        except ValueError:
            transferred = 0
        return (
            text.get("copyStatusType", ""),
            text.get("errorMessage", ""),
            transferred,
        )

    async def _async_clear_load_status(self) -> None:
        """Clear the transfer record, as the UI does once it finishes."""
        try:
            await self._request(
                "wcd",
                body=(
                    "<?xml version='1.0' encoding='utf-8'?>"
                    '<DeviceConfiguration set="set">'
                    '<LoadStatus action="delete" delete="delete">'
                    "<Entry><unitID>0</unitID></Entry>"
                    "</LoadStatus></DeviceConfiguration>"
                ),
            )
        except NetgearError:
            # Cosmetic: a stale record only affects the switch's own UI.
            _LOGGER.debug("Could not clear LoadStatus", exc_info=True)

    async def _async_post_archive(self, image: bytes, filename: str) -> None:
        """POST the firmware archive as the UI's upload iframe does.

        Not routed through _request: the body is multipart rather than XML,
        and the switch answers a 302 on success, which _attempt_request would
        read as an expired session.
        """
        form = aiohttp.FormData()
        form.add_field("restoreUrl", "")
        form.add_field("errorCollector", "")
        form.add_field("rlCopyFreeHistoryIndex$scalar", "1")
        form.add_field("rlCopyDestinationFileType", _ARCHIVE_FILE_TYPE)
        form.add_field("rlCopyFreeHistoryIndex", "1")
        form.add_field(
            "srcFileName",
            image,
            filename=filename,
            content_type="application/octet-stream",
        )
        try:
            async with self._get_session().post(
                self._url(_UPLOAD_PATH),
                data=form,
                headers={"Cookie": f"sessionID={self._cookie}"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=_FIRMWARE_UPLOAD_TIMEOUT),
            ) as resp:
                # 302 is the switch's normal "upload accepted" answer.
                if resp.status not in (200, 302):
                    resp.raise_for_status()
                await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Firmware upload failed: {err}") from err

    async def _async_upload_firmware(
        self,
        image: bytes,
        filename: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Upload the archive, reporting real progress while it transfers.

        The switch's own UI submits the upload from an iframe and polls
        {LoadStatus} alongside it, so it tolerates being read mid-transfer.
        Doing the same is what makes the bar move: bytesTransfered is the one
        genuine progress signal either backend exposes.
        """
        upload = asyncio.create_task(self._async_post_archive(image, filename))
        try:
            while not upload.done():
                await asyncio.sleep(_UPLOAD_POLL_SECONDS)
                try:
                    _status, _err, sent = await self._async_load_status()
                except NetgearError:
                    continue  # the switch is busy; the upload task decides
                if sent and progress is not None:
                    percent = min(sent * 100 // max(len(image), 1), 100)
                    progress(20 + percent * 40 // 100)  # 20..60
        finally:
            await upload  # surface upload errors, and never leave it orphaned

        status, message, _sent = await self._async_load_status()
        if status in _COPY_FAILED:
            await self._async_clear_load_status()
            raise NetgearError(
                f"Switch rejected the firmware: {message or 'transfer failed'}"
            )
        if status != _COPY_DONE:
            await self._async_clear_load_status()
            raise NetgearError(
                f"Firmware upload did not complete (copyStatusType={status or '?'})"
            )
        await self._async_clear_load_status()

    async def _async_activate_image(self, slot: str, description: str) -> None:
        """Mark a slot as the image to boot from (next-active)."""
        number = slot.removeprefix("image")
        current = (await self.async_get_image_status()).current_active
        root = await self._request(
            "wcd",
            body=(
                "<?xml version='1.0' encoding='utf-8'?>"
                '<DeviceConfiguration set="set">'
                '<ImageUnitList action="set" set="set">'
                f"<Entry><nextBootImage>{number}</nextBootImage><unitID>1</unitID>"
                f"<currentImage>{current.removeprefix('image')}</currentImage>"
                f"<image{number}Description>{escape(description)}"
                f"</image{number}Description>"
                "</Entry></ImageUnitList></DeviceConfiguration>"
            ),
        )
        code = root.findtext(".//ActionStatus/statusCode", "").strip()
        if code not in ("", "0"):
            status = root.findtext(".//ActionStatus/statusString", "").strip()
            raise NetgearError(f"Could not activate {slot}: {status or code}")

    async def _async_reboot(self) -> None:
        """Reboot the switch. Mirrors the Device Reboot screen's Reload set.

        The switch usually drops the connection as it goes down, so a failure
        here means the reboot took, not that it failed.
        """
        try:
            await self._request(
                "wcd",
                body=(
                    "<?xml version='1.0' encoding='utf-8'?>"
                    '<DeviceConfiguration set="set">'
                    '<Reload action="set" set="set">'
                    "<UnitList><UnitEntry><unitID>0</unitID></UnitEntry></UnitList>"
                    "</Reload></DeviceConfiguration>"
                ),
            )
        except NetgearError:
            _LOGGER.debug("Reboot request did not answer (expected)", exc_info=True)
        self._cookie = None

    async def _async_wait_for_firmware(
        self, version: str, progress: Callable[[int], None] | None = None
    ) -> None:
        """Poll until the rebooted switch reports `version`.

        Early polls may still reach the old firmware (the switch answers for a
        few seconds before it goes down), so a mismatch keeps waiting. Progress
        walks 80 -> 95 as the retry budget is spent.
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

        Unlike the /base/ UI, this form has no slot field — the switch writes
        the archive to the inactive slot itself, so the running firmware stays
        flashed as a rollback either way. The reboot drops PoE and the
        switch's own link for a minute or more.
        """

        def report(percent: int) -> None:
            if progress is not None:
                progress(percent)

        status = await self.async_get_image_status()
        target = status.inactive
        _LOGGER.info(
            "Uploading firmware %s to %s (running %s from %s; the switch "
            "writes to %s itself)",
            version,
            self.host,
            status.versions.get(status.current_active, "?"),
            status.current_active,
            target,
        )
        report(20)
        await self._async_upload_firmware(image, filename, progress)

        staged = await self.async_get_image_status()
        if staged.versions.get(target) != version:
            raise NetgearError(
                f"Upload finished but slot {target} reports "
                f"{staged.versions.get(target) or 'nothing'}, expected {version}"
            )
        report(65)

        await self._async_activate_image(target, version)
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
) -> NetgearAnyApi:
    """Return the right client for the switch's firmware generation.

    The three generations answer the root URL differently:

    * Legacy xui (GS516TP) 302-redirects to its per-device /csbe<id>/ prefix.
    * The classic /base/ UI (GS110TP) serves a login form posting to
      /base/main_login.html.
    * The newer JSON-CGI firmware serves its own login page directly.
    """
    timeout = aiohttp.ClientTimeout(total=10)
    probe_session = session or aiohttp.ClientSession(timeout=timeout)
    try:
        try:
            # async with: the legacy branch never reads the body, so without it
            # the response would keep a connection out of a caller-supplied
            # session's pool.
            async with probe_session.get(
                f"http://{host}/",  # NOSONAR — probing the HTTP-only web UI
                allow_redirects=False,
            ) as resp:
                match = _PREFIX_RE.search(resp.headers.get("Location", ""))
                # Only the non-redirecting generations need their body read.
                body = "" if match else await resp.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetgearError(f"Cannot connect to {host}: {err}") from err
    finally:
        if session is None:
            await probe_session.close()

    if match is not None:
        return NetgearLegacyApi(
            host=host, password=password, session=session, prefix=match.group(1)
        )
    if _BASE_UI_RE.search(body):
        return NetgearBaseUiApi(host=host, password=password, session=session)
    return NetgearPoeApi(host=host, password=password, session=session)
