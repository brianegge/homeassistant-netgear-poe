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
  modulus[37:] (home.html: sess.substring(37, sess.length)). The older
  firmware's parser drops a trailing byte; keeping that here would
  truncate the RSA modulus and break the X-CSRF-XSID header.
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

from .api import NetgearError, NetgearPoeApi, form_body

_LOGGER = logging.getLogger(__name__)


class NetgearJsonV2Api(NetgearPoeApi):
    """Async PoE client for the aj4 JSON-CGI web UI (GS728TPPv3 6.2.x)."""

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
        """The modulus is the whole remainder — no trailing byte to drop."""
        return sess[:32], sess[32:37], sess[37:]

    def _login_auth_ok(self, result: dict[str, Any]) -> bool:
        """login.html only checks for status == "error", not for "ok"."""
        return str(result.get("status", "")).lower() != "error"

    async def _request(
        self, cgi: str, cmd: str, body: str | None = None
    ) -> dict[str, Any]:
        result = await super()._request(cgi, cmd, body)
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

    async def async_logout(self) -> None:
        await super().async_logout()
        self._xsrf = "null"
