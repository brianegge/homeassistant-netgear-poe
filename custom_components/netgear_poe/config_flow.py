"""Config flow for Netgear PoE Switch."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD

from .api import NetgearAuthError, NetgearError, NetgearPoeApi
from .const import DOMAIN

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_connection(host: str, password: str) -> str:
    """Log in, verify PoE ports exist, and return a title. Raises on failure."""
    api = NetgearPoeApi(host=host, password=password)
    try:
        sys_name, model = await api.async_get_info()
        data = await api.async_get_data()
        if not data.ports:
            raise NetgearError("No PoE ports found")
        return sys_name or model or host
    finally:
        await api.async_close()


class NetgearPoeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Netgear PoE Switch."""

    VERSION = 1

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

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
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
                CONF_HOST: reauth_entry.data[CONF_HOST],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            if await self._async_validate(full_input, errors) is not None:
                return self.async_update_reload_and_abort(
                    reauth_entry, data=full_input
                )

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

        if user_input is not None:
            if await self._async_validate(user_input, errors) is not None:
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
                }
            ),
            errors=errors,
        )
