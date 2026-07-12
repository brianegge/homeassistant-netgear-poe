"""Tests for the Netgear PoE config flow."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netgear_poe.api import NetgearAuthError, NetgearError
from custom_components.netgear_poe.const import DOMAIN

from .conftest import MOCK_CONFIG, MOCK_SYS_NAME


async def test_user_flow_success(hass: HomeAssistant, mock_api: MagicMock) -> None:
    """Test a successful user flow creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == MOCK_SYS_NAME
    assert result["data"] == MOCK_CONFIG


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, mock_api: MagicMock
) -> None:
    """Test connection failure shows an error and allows retry."""
    mock_api.async_get_info.side_effect = NetgearError("timeout")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    mock_api.async_get_info.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_invalid_auth(hass: HomeAssistant, mock_api: MagicMock) -> None:
    """Test a wrong password shows invalid_auth."""
    mock_api.async_get_info.side_effect = NetgearAuthError("bad password")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the flow aborts when the switch is already configured."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reauth updates the password."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"password": "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data["password"] == "new-password"
    await hass.async_block_till_done()


async def test_reconfigure_flow(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reconfiguring the entry."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    new_config = {**MOCK_CONFIG, "password": "new-password"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=new_config
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data["password"] == "new-password"
    await hass.async_block_till_done()
