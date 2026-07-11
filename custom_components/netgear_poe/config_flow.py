"""Config flow for Netgear PoE Switch."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST

from .api import NetgearPoeApi, SnmpError
from .const import (
    CONF_COMMUNITY,
    CONF_WRITE_COMMUNITY,
    DEFAULT_COMMUNITY,
    DOMAIN,
)


async def _validate_connection(
    host: str, community: str, write_community: str
) -> str:
    """Connect, verify PoE ports exist, and return a title. Raises on failure."""
    api = NetgearPoeApi(host=host, community=community, write_community=write_community)
    try:
        sys_name, _ = await api.async_get_info()
        data = await api.async_get_data()
        if not data.ports:
            raise SnmpError("No PoE ports found")
        return sys_name or host
    finally:
        await api.async_close()


class NetgearPoeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Netgear PoE Switch."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            community = user_input[CONF_COMMUNITY]
            write_community = user_input.get(CONF_WRITE_COMMUNITY, "")

            try:
                title = await _validate_connection(host, community, write_community)
            except SnmpError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_HOST: host,
                        CONF_COMMUNITY: community,
                        CONF_WRITE_COMMUNITY: write_community,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_COMMUNITY, default=DEFAULT_COMMUNITY): str,
                    vol.Optional(CONF_WRITE_COMMUNITY, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            community = user_input[CONF_COMMUNITY]
            write_community = user_input.get(CONF_WRITE_COMMUNITY, "")

            try:
                await _validate_connection(host, community, write_community)
            except SnmpError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data={
                        CONF_HOST: host,
                        CONF_COMMUNITY: community,
                        CONF_WRITE_COMMUNITY: write_community,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=reconfigure_entry.data.get(CONF_HOST)
                    ): str,
                    vol.Required(
                        CONF_COMMUNITY,
                        default=reconfigure_entry.data.get(
                            CONF_COMMUNITY, DEFAULT_COMMUNITY
                        ),
                    ): str,
                    vol.Optional(
                        CONF_WRITE_COMMUNITY,
                        default=reconfigure_entry.data.get(CONF_WRITE_COMMUNITY, ""),
                    ): str,
                }
            ),
            errors=errors,
        )
