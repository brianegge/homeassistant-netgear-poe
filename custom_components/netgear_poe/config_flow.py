"""Config flow for Netgear PoE Switch."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from . import resolve_trap_mode
from .api import NetgearAuthError, NetgearError
from .api_legacy import async_detect_api
from .const import (
    CONF_COMMUNITY,
    CONF_TRAP_BRIDGE_HOST,
    CONF_TRAP_MODE,
    DOMAIN,
    TRAP_MODE_LOCAL,
    TRAP_MODES,
)

_TRAP_SCHEMA = {
    vol.Optional(CONF_TRAP_MODE, default=TRAP_MODE_LOCAL): vol.In(TRAP_MODES),
    vol.Optional(CONF_TRAP_BRIDGE_HOST, default=""): str,
}

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_COMMUNITY, default=""): str,
        **_TRAP_SCHEMA,
    }
)


async def _validate_connection(host: str, password: str) -> str:
    """Log in, read the switch, and return a title. Raises on failure.

    A successful get_data with no ports is fine — a non-PoE model like the
    GS108Tv2 still gets a firmware-update entity — so only a login or
    connection failure blocks setup, not the absence of PoE.
    """
    api = await async_detect_api(host=host, password=password)
    try:
        info = await api.async_get_info()
        await api.async_get_data()
        return info.name or info.model or host
    finally:
        await api.async_close()


class NetgearPoeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Netgear PoE Switch."""

    VERSION = 1

    _discovered: dict[str, Any]

    async def _async_validate(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> str | None:
        try:
            return await _validate_connection(
                user_input[CONF_HOST], user_input[CONF_PASSWORD]
            )
        except NetgearAuthError:
            errors["base"] = "invalid_auth"
        except NetgearError:
            errors["base"] = "cannot_connect"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            title = await self._async_validate(user_input, errors)
            if title is not None:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle a switch discovered via NSDP."""
        return await self._async_handle_discovery(discovery_info)

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a switch discovered via SSDP.

        Some models never speak NSDP (the GS728TPPv3 for one) but announce
        themselves over UPnP with modelDescription "NETGEAR Switch". The web
        UI host comes from presentationURL and the MAC is the tail of the
        device UUID (uuid:...-<12 hex digits of MAC>).
        """
        upnp = discovery_info.upnp
        host = urlparse(
            upnp.get("presentationURL") or discovery_info.ssdp_location or ""
        ).hostname
        mac = upnp.get("UDN", "").rpartition("-")[2]
        if not host or len(mac) != 12:
            return self.async_abort(reason="cannot_connect")
        return await self._async_handle_discovery(
            {
                CONF_HOST: host,
                "mac": mac,
                "model": upnp.get("modelName", ""),
                "name": upnp.get("friendlyName", ""),
            }
        )

    async def _async_handle_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Dedupe a discovered switch and ask for its password."""
        host = discovery_info[CONF_HOST]
        await self.async_set_unique_id(format_mac(discovery_info["mac"]))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        # Also skip if the same host was already added manually.
        if any(
            entry.data.get(CONF_HOST) == host for entry in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")

        self._discovered = discovery_info
        name = discovery_info.get("name") or host
        model = discovery_info.get("model") or "switch"
        self.context["title_placeholders"] = {"name": f"{name} ({model})"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the password to finish setting up a discovered switch."""
        errors: dict[str, str] = {}
        discovered = self._discovered

        if user_input is not None:
            full_input = {
                CONF_HOST: discovered[CONF_HOST],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_COMMUNITY: user_input.get(CONF_COMMUNITY, ""),
                CONF_TRAP_MODE: user_input.get(CONF_TRAP_MODE, TRAP_MODE_LOCAL),
                CONF_TRAP_BRIDGE_HOST: user_input.get(CONF_TRAP_BRIDGE_HOST, ""),
            }
            title = await self._async_validate(full_input, errors)
            if title is not None:
                return self.async_create_entry(title=title, data=full_input)

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_COMMUNITY, default=""): str,
                    **_TRAP_SCHEMA,
                }
            ),
            description_placeholders={
                "name": discovered.get("name") or discovered[CONF_HOST],
                "model": discovered.get("model") or "",
                "host": discovered[CONF_HOST],
            },
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthorization when the password changes."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            full_input = {
                **reauth_entry.data,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            if await self._async_validate(full_input, errors) is not None:
                return self.async_update_reload_and_abort(reauth_entry, data=full_input)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None and (
            await self._async_validate(user_input, errors) is not None
        ):
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                reconfigure_entry, data=user_input
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=reconfigure_entry.data.get(CONF_HOST)
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_COMMUNITY,
                        default=reconfigure_entry.data.get(CONF_COMMUNITY, ""),
                    ): str,
                    vol.Optional(
                        CONF_TRAP_MODE,
                        default=resolve_trap_mode(reconfigure_entry),
                    ): vol.In(TRAP_MODES),
                    vol.Optional(
                        CONF_TRAP_BRIDGE_HOST,
                        default=reconfigure_entry.data.get(CONF_TRAP_BRIDGE_HOST, ""),
                    ): str,
                }
            ),
            errors=errors,
        )
