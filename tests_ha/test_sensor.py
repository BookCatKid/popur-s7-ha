"""Tests for Popur diagnostic sensors."""

from types import SimpleNamespace

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.popur_s7.const import DOMAIN

from .conftest import DEV_ID, FakePopur


async def test_lan_ip_sensor_uses_public_config(
    hass: HomeAssistant, popur: FakePopur, setup_entry
) -> None:
    """Show the configured LAN IP and handle cloud-only devices."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DEV_ID}_lan_ip")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == STATE_UNKNOWN

    # Only the public transport interface is available to the integration.
    popur.client.transport = SimpleNamespace(
        primary=SimpleNamespace(config=SimpleNamespace(host="192.0.2.10"))
    )
    await setup_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "192.0.2.10"
