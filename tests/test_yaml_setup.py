"""Tests for the configuration.yaml entry that enables discovery."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.netgear_poe.const import DOMAIN

NO_OPTIONS_ERROR = "does not support any configuration parameters"


async def test_bare_yaml_key_is_accepted(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A bare `netgear_poe:` sets the integration up without complaint."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: None})
    await hass.async_block_till_done()
    assert NO_OPTIONS_ERROR not in caplog.text


async def test_yaml_options_are_flagged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The YAML key takes no options; switches are configured in the UI."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {"host": "10.0.0.2"}})
    await hass.async_block_till_done()
    assert NO_OPTIONS_ERROR in caplog.text
