"""Regression tests for event-driven Popur LAN address changes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, SOURCE_DHCP
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.popur_s7.const import (
    CONF_DISCOVERED_HOSTS,
    CONF_HOST,
    CONF_INSTALL_ID,
    DOMAIN,
)
from pypopur.mobile import AccountDevice
from pypopur.transport import FallbackTransport

from .conftest import DEV_ID, FakePopur

MAC = "00:33:7a:ab:cd:ef"
OLD_IP = "192.0.2.10"
NEW_IP = "192.0.2.11"
DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "pw", CONF_INSTALL_ID: "install"}


async def discover(hass: HomeAssistant, mac: str = "00337aabcdef"):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=DhcpServiceInfo(ip=NEW_IP, macaddress=mac, hostname="popur"),
    )


async def test_known_device_ip_change_reloads_without_rescanning(
    hass: HomeAssistant, popur: FakePopur
) -> None:
    """A real config-entry reload uses the new IP, not another subnet scan."""
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="account-id")
    entry.add_to_hass(hass)
    popur.hass = hass
    popur.account.home_devices.return_value = [
        AccountDevice(device_id=DEV_ID, product_id="p", name="Litter", local_key="k", mac=MAC)
    ]

    def local(host, *_args, **_kwargs):
        return SimpleNamespace(
            config=SimpleNamespace(host=host), connect=AsyncMock(), protocol_version="3.5"
        )

    def client(transport):
        popur.client.transport = transport
        return popur.client

    with (
        patch("custom_components.popur_s7.ThingMobileApi"),
        patch("custom_components.popur_s7.MobileAppProfile"),
        patch("custom_components.popur_s7.PopurAccount", return_value=popur.account),
        patch("custom_components.popur_s7.PopurClient", side_effect=client),
        patch("custom_components.popur_s7.LocalTuyaTransport", side_effect=local),
        patch("custom_components.popur_s7.find_lan_hosts", return_value=[OLD_IP]) as scan,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert isinstance(popur.client.transport, FallbackTransport)
        assert popur.client.transport.primary.config.host == OLD_IP
        scan.assert_awaited_once_with(MAC)

        result = await discover(hass)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.unique_id == "account-id"
        assert entry.data == {**DATA, CONF_DISCOVERED_HOSTS: {MAC: NEW_IP}}
        assert popur.client.transport.primary.config.host == NEW_IP
        scan.assert_awaited_once()

        assert (await discover(hass))["reason"] == "already_configured"
        await hass.async_block_till_done()
        assert popur.account.login.await_count == 2
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("configured_mac", "override", "local_key"),
    [
        ("00:33:7a:ff:ff:ff", "", "key"),
        (MAC, OLD_IP, "key"),
        (MAC, "", ""),
    ],
)
async def test_dhcp_does_not_replace_unrelated_or_manually_configured_address(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    configured_mac: str,
    override: str,
    local_key: str,
) -> None:
    entry = setup_entry
    hass.config_entries.async_update_entry(entry, data={**DATA, CONF_HOST: override})
    entry.runtime_data.account_devices[:] = [
        SimpleNamespace(mac=configured_mac, local_key=local_key)
    ]
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        patch("custom_components.popur_s7.config_flow.asyncio.open_connection") as probe,
    ):
        result = await discover(hass)
    assert result["reason"] == "already_configured"
    assert entry.data == {**DATA, CONF_HOST: override}
    reload.assert_not_called()
    probe.assert_not_called()


async def test_new_dhcp_setup_keeps_address_separate_from_manual_override(
    hass: HomeAssistant,
) -> None:
    writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
    with (
        patch(
            "custom_components.popur_s7.config_flow.asyncio.open_connection",
            return_value=(None, writer),
        ),
        patch("custom_components.popur_s7.config_flow.MobileAppProfile"),
        patch("custom_components.popur_s7.config_flow.ThingMobileApi"),
        patch("custom_components.popur_s7.config_flow.PopurAccount") as account,
        patch.object(hass.config_entries, "async_setup", return_value=True),
    ):
        account.return_value.login = AsyncMock(return_value=SimpleNamespace(uid="account-id"))
        result = await discover(hass)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: DATA[CONF_EMAIL], CONF_PASSWORD: DATA[CONF_PASSWORD]}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == ""
    assert result["data"][CONF_DISCOVERED_HOSTS] == {MAC: NEW_IP}
