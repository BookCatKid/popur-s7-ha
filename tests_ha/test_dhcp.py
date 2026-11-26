"""Regression tests for event-driven Popur LAN address changes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, SOURCE_DHCP, SOURCE_RECONFIGURE
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

    verified = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), protocol_version="3.5")
    with (
        patch(
            "custom_components.popur_s7.config_flow.LocalTuyaTransport", return_value=verified
        ) as probe,
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
        probe.assert_called_once_with(NEW_IP, DEV_ID, "k", timeout=3.0)
        verified.connect.assert_awaited_once()
        verified.close.assert_awaited_once()
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


async def test_failed_dhcp_handshake_keeps_previous_address_and_coalesces_attempts(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
) -> None:
    entry = setup_entry
    data = {**DATA, CONF_DISCOVERED_HOSTS: {MAC: OLD_IP}}
    hass.config_entries.async_update_entry(entry, data=data)
    entry.runtime_data.account_devices[:] = [
        AccountDevice(device_id=DEV_ID, product_id="p", name="Litter", local_key="k", mac=MAC)
    ]
    transport = SimpleNamespace(connect=AsyncMock(side_effect=OSError), close=AsyncMock())
    with (
        patch(
            "custom_components.popur_s7.config_flow.LocalTuyaTransport", return_value=transport
        ) as probe,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        assert (await discover(hass))["reason"] == "already_configured"
        assert (await discover(hass))["reason"] == "already_configured"
    assert entry.data == data
    probe.assert_called_once_with(NEW_IP, DEV_ID, "k", timeout=3.0)
    transport.close.assert_awaited_once()
    reload.assert_not_called()


async def test_stale_dhcp_hint_falls_back_to_setup_discovery() -> None:
    from custom_components.popur_s7 import _local_transport

    device = AccountDevice(device_id=DEV_ID, product_id="p", name="Litter", local_key="k", mac=MAC)

    def local(host, *_args, **_kwargs):
        return SimpleNamespace(
            config=SimpleNamespace(host=host),
            protocol_version="3.5",
            connect=AsyncMock(side_effect=OSError if host == OLD_IP else None),
        )

    with (
        patch("custom_components.popur_s7.LocalTuyaTransport", side_effect=local) as transport,
        patch("custom_components.popur_s7.find_lan_hosts", return_value=[OLD_IP, NEW_IP]) as scan,
    ):
        assert (await _local_transport(device, None, OLD_IP)).config.host == NEW_IP
    assert [call.args[0] for call in transport.call_args_list] == [OLD_IP, NEW_IP]
    scan.assert_awaited_once_with(MAC)


async def test_no_popur_setup_for_unrelated_registered_device(hass: HomeAssistant) -> None:
    with patch("custom_components.popur_s7.config_flow.asyncio.open_connection") as tcp:
        result = await discover(hass, "001122aabbcc")
    assert result["reason"] == "not_popur_device"
    tcp.assert_not_called()


async def test_reconfigure_can_clear_legacy_fixed_ip(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, CONF_HOST: OLD_IP}, unique_id="account-id")
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["type"] is FlowResultType.FORM
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: ""})
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {**DATA, CONF_HOST: ""}
    assert entry.unique_id == "account-id"
    reload.assert_awaited_once_with(entry.entry_id)


async def test_dhcp_waits_for_reload_lock(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    entry = setup_entry
    entry.runtime_data.account_devices[:] = [
        AccountDevice(device_id=DEV_ID, product_id="p", name="Litter", local_key="k", mac=MAC)
    ]
    transport = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), protocol_version="3.5")
    with (
        patch("custom_components.popur_s7.config_flow.LocalTuyaTransport", return_value=transport),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        async with entry.setup_lock:
            task = asyncio.create_task(discover(hass))
            await asyncio.sleep(0)
            transport.connect.assert_not_awaited()
            assert not task.done()
        assert (await task)["reason"] == "already_configured"
    assert entry.data[CONF_DISCOVERED_HOSTS] == {MAC: NEW_IP}
    reload.assert_called_once_with(entry.entry_id)
