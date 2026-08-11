"""Client for the redesigned Bootstrap/Backbone JSON-CGI web UI ("aj4").

The GS728TPPv3 (firmware 6.2.x) ships a redesigned web UI whose root page
bootstraps to login.html?aj4=<ms>&bj4=md5(query) — that aj4 asset-version
parameter is the detection marker. The wire protocol was reverse-engineered
from the switch's own login.html, home.html, js/utility.js and js/url.js
(fetched from a live V6.2.0.36 GS728TPPv3):

* URLs and integrity hashes are unchanged: /cgi/get.cgi and /cgi/set.cgi
  with cmd=<cmd>&dummy=<ms>&bj4=md5(query) (utility.js urlParamHash()).
* The login handshake is the authId flow NetgearPoeApi already speaks:
  home_loginAuth returns an authId that is POSTed back to home_loginStatus
  until the session is granted. login.html only treats status == "error"
  as a rejected password, so this driver does the same instead of
  requiring status == "ok".
* The b64-decoded session splits tabid[0:32] + exponent[32:37] +
  modulus[37:] (home.html: sess.substring(37, sess.length)), and the blob
  ends with a C-string NUL after the modulus (observed live). jsbn's
  parser skips non-hex characters, so the modulus here is the remainder
  with non-hex bytes stripped — not a fixed [:-1] drop like the older
  firmware's parser, which happens to match today only because the junk
  is exactly one byte.
* Every set.cgi body carries an xsrf token (utility.js formDataGet()
  appends xsrf=<id> to each form). It starts as the literal "null"
  (login.html: var xsrfId = null), the real value arrives in home_home's
  data.xsrf after login, and any response carrying an xsrf field rotates
  it (_resp_status_print). A write with a stale token is answered with
  logout/invalidCsrf, so the token is harvested from every response.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from .api import NetgearError, NetgearPoeApi, form_body

_GET_CGI = "get.cgi"
_SET_CGI = "set.cgi"

_LOGGER = logging.getLogger(__name__)


# The aj4 port_port wire encoding for link settings, per the switch's own
# switch_ports_port.html: speed is a string ("Auto" or a bare Mbit/s number;
# comma lists like "10,100" are multi-speed advertisements), duplex is an
# enum (0 half, 1 full, 2 auto), autoNego a 0/1 flag.
_SPEED_WIRE = {"auto": "Auto", "10": "10", "100": "100", "1000": "1000"}
_DUPLEX_WIRE = {"half": 0, "full": 1, "auto": 2}


class NetgearJsonV2Api(NetgearPoeApi):
    """Async PoE client for the aj4 JSON-CGI web UI (GS728TPPv3 6.2.x)."""

    supports_port_speed = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The body-level CSRF token; "null" mirrors the login page's
        # uninitialized xsrfId, which the switch accepts pre-login.
        self._xsrf = "null"

    def _form_body(self, fields: dict[str, Any]) -> str:
        """Carry the live xsrf token in every set.cgi body.

        Callers that spell out xsrf themselves (the base class uses
        "undefined") get it replaced in place; otherwise it is appended
        last, matching formDataGet()'s field order.
        """
        return form_body({**fields, "xsrf": self._xsrf})

    def _parse_sess(self, sess: str) -> tuple[str, str, str]:
        """The modulus is the remainder of the blob, minus any non-hex bytes.

        A real GS728TPPv3 V6.2.0.36 terminates the blob with a C-string NUL
        after the 256 hex chars of the modulus. home.html feeds the whole
        remainder to jsbn, whose parser silently skips non-hex characters —
        so mirror that instead of assuming a fixed amount of trailing junk
        (the base class's [:-1] and this NUL agree today, but only by luck).
        """
        modulus = "".join(c for c in sess[37:] if c in "0123456789abcdefABCDEF")
        return sess[:32], sess[32:37], modulus

    def _login_auth_ok(self, result: dict[str, Any]) -> bool:
        """login.html only checks for status == "error", not for "ok"."""
        return str(result.get("status", "")).lower() != "error"

    async def _request(
        self,
        cgi: str,
        cmd: str,
        body: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await super()._request(cgi, cmd, body, params)
        self._harvest_xsrf(result)
        return result

    def _harvest_xsrf(self, result: dict[str, Any]) -> None:
        """Pick up a rotated xsrf token from a response.

        Set responses carry it at the top level; home_home nests it in data.
        """
        xsrf = result.get("xsrf")
        if not xsrf:
            data = result.get("data")
            if isinstance(data, dict):
                xsrf = data.get("xsrf")
        if xsrf:
            self._xsrf = str(xsrf)

    async def async_login(self) -> None:
        """Log in, then fetch the initial xsrf token from home_home."""
        self._xsrf = "null"
        await super().async_login()
        # Reads work without it, so a hiccup here only defers writes to the
        # next token a response hands us rather than failing the login.
        try:
            await self._request("get.cgi", "home_home")
        except NetgearError:
            _LOGGER.debug(
                "Could not fetch the xsrf token from %s", self.host, exc_info=True
            )

    async def _async_port_row(self, port: int) -> dict[str, Any]:
        """Return the port_port row for a port, matched by its ifindex."""
        result = await self._authed_request(_GET_CGI, "port_port")
        for index, candidate in enumerate(result.get("data", {}).get("ports", [])):
            if int(candidate.get("ifindex", index + 1)) == port:
                return candidate
        raise NetgearError(f"Port {port} not found")

    async def async_set_port_name(self, port: int, name: str) -> None:
        """Set a port's description (aj4 wire format).

        This firmware renamed the port-edit exchange: reads and writes both
        go through cmd=port_port (the older generation writes to
        port_portEdit), the row fields are admin/autoNego/speed/duplex/
        flowCtrl/trap (not adminStatus/adminSpeed/...), and the edited row
        is addressed by a 0-based selEntry index — all per the switch's own
        switch_ports_port.html formEdit(). The base implementation would
        refuse with "Port settings incomplete" on every port here.
        """
        row = await self._async_port_row(port)
        fields = {
            "descp": quote(name, safe=""),
            "trap": row.get("trap", 1),
            "admin": row.get("admin", 1),
            "autoNego": row.get("autoNego", 1),
            "speed": row.get("speed", "Auto"),
            "duplex": row.get("duplex", 2),
            "flowCtrl": row.get("flowCtrl", 0),
            "selEntry": port - 1,
        }
        result = await self._authed_request(
            _SET_CGI, "port_port", self._form_body(fields)
        )
        if result.get("status") != "ok":
            raise NetgearError(f"Port name set failed: {result}")
        if name:
            self._port_names[port] = name
        else:
            self._port_names.pop(port, None)

    async def async_set_port_speed(
        self, port: int, speed: str, duplex: str = "auto", autoneg: bool = True
    ) -> None:
        """Set a port's link speed and duplex (aj4 wire format).

        With autoneg on, a specific speed restricts what the port
        advertises — the peer keeps negotiating and lands on that rate with
        the right duplex, so this is the safe way to pin a marginal link
        (no duplex-mismatch risk). autoneg off hard-forces speed/duplex and
        requires both to be explicit, mirroring the switch UI's validation.
        The description and every other port setting are preserved.

        The switch's own page also refuses speeds other than Auto/1000 on
        its fiber (SFP) ports; that rule lives in client-side JS, so here
        the firmware gets the final say — a write it ignores fails the
        read-back verification below.
        """
        if speed not in _SPEED_WIRE or duplex not in _DUPLEX_WIRE:
            raise NetgearError(f"Invalid speed {speed!r} / duplex {duplex!r}")
        if not autoneg and (speed == "auto" or duplex == "auto"):
            raise NetgearError(
                "Disabling autonegotiation requires an explicit speed and duplex"
            )
        if speed == "1000" and duplex == "half":
            raise NetgearError("1000M does not support half duplex")
        row = await self._async_port_row(port)
        fields = {
            "descp": quote(str(row.get("descp", "")), safe=""),
            "trap": row.get("trap", 1),
            "admin": row.get("admin", 1),
            "autoNego": 1 if autoneg else 0,
            "speed": _SPEED_WIRE[speed],
            "duplex": _DUPLEX_WIRE[duplex],
            "flowCtrl": row.get("flowCtrl", 0),
            "selEntry": port - 1,
        }
        result = await self._authed_request(
            _SET_CGI, "port_port", self._form_body(fields)
        )
        if result.get("status") != "ok":
            raise NetgearError(f"Port speed set failed: {result}")
        after = await self._async_port_row(port)
        wanted = (1 if autoneg else 0, _SPEED_WIRE[speed], _DUPLEX_WIRE[duplex])
        got = (
            int(after.get("autoNego", -1)),
            str(after.get("speed", "")),
            int(after.get("duplex", -1)),
        )
        if got != wanted:
            raise NetgearError(
                f"Port {port} speed did not stick (wanted autoNego/speed/duplex "
                f"{wanted}, switch reports {got})"
            )

    async def async_logout(self) -> None:
        await super().async_logout()
        self._xsrf = "null"
