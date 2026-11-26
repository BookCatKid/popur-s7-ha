"""Tests for DHCP-driven Popur LAN discovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, SOURCE_DHCP, SOURCE_RECONFIGURE
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.popur_s7.const import CONF_HOST, CONF_INSTALL_ID, DOMAIN
from pypopur.mobile import AccountDevice
from pypopur.transport import FallbackTransport

from .conftest import DEV_ID, FakePopur

MAC = "00:33:7a:ab:cd:ef"
OLD_IP = "192.0.2.10"
NEW_IP = "192.0.2.11"
DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "pw", CONF_INSTALL_ID: "install"}


async def discover(hass: HomeAssistant, ip: str = NEW_IP, mac: str = "00337aabcdef"):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=DhcpServiceInfo(ip=ip, macaddress=mac, hostname="popur"),
    )


def device(mac: str = MAC, local_key: str = "k") -> AccountDevice:
    return AccountDevice(
        device_id=DEV_ID, product_id="p", name="Litter", local_key=local_key, mac=mac
    )


async def test_dhcp_change_reloads_with_fresh_lan_discovery(
    hass: HomeAssistant, popur: FakePopur
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="account-id")
    entry.add_to_hass(hass)
    popur.hass = hass
    popur.account.home_devices.return_value = [device()]

    def local(host, *_args, **_kwargs):
        return SimpleNamespace(
            config=SimpleNamespace(host=host),
            connect=AsyncMock(),
            close=AsyncMock(),
            protocol_version="3.5",
        )

    def client(transport):
        popur.client.transport = transport
        return popur.client

    with (
        patch("custom_components.popur_s7.ThingMobileApi"),
        patch("custom_components.popur_s7.MobileAppProfile"),
        patch("custom_components.popur_s7.PopurAccount", return_value=popur.account),
        patch("custom_components.popur_s7.PopurClient", side_effect=client),
        patch("custom_components.popur_s7.LocalTuyaTransport", side_effect=local) as transports,
        patch(
            "custom_components.popur_s7.find_lan_hosts", side_effect=[[OLD_IP], [NEW_IP]]
        ) as scan,
        patch("custom_components.popur_s7.config_flow.asyncio.open_connection") as tcp,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert popur.client.transport.primary.config.host == OLD_IP

        result = await discover(hass)
        assert result["reason"] == "already_configured"
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert popur.client.transport.primary.config.host == NEW_IP
        assert scan.await_count == 2
        assert [call.args[0] for call in transports.call_args_list] == [OLD_IP, NEW_IP]
        assert entry.data == DATA
        tcp.assert_not_called()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unchanged_address_does_not_reload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    entry = setup_entry
    entry.runtime_data.account_devices[:] = [device()]
    entry.runtime_data.clients[DEV_ID].transport = FallbackTransport(
        SimpleNamespace(config=SimpleNamespace(host=OLD_IP)), SimpleNamespace()
    )

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert (await discover(hass, ip=OLD_IP))["reason"] == "already_configured"
    reload.assert_not_called()


async def test_cloud_only_device_rediscovered_on_dhcp(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    entry = setup_entry
    entry.runtime_data.account_devices[:] = [device()]

    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        patch("custom_components.popur_s7.config_flow.asyncio.open_connection") as tcp,
    ):
        assert (await discover(hass))["reason"] == "already_configured"
    reload.assert_called_once_with(entry.entry_id)
    tcp.assert_not_called()


@pytest.mark.parametrize(
    ("configured_mac", "override", "local_key"),
    [
        ("00:33:7a:ff:ff:ff", "", "k"),
        (MAC, OLD_IP, "k"),
        (MAC, "", ""),
    ],
)
async def test_unrelated_or_fixed_devices_do_not_reload(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    configured_mac: str,
    override: str,
    local_key: str,
) -> None:
    entry = setup_entry
    hass.config_entries.async_update_entry(entry, data={**DATA, CONF_HOST: override})
    entry.runtime_data.account_devices[:] = [device(configured_mac, local_key)]

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert (await discover(hass))["reason"] == "already_configured"
    reload.assert_not_called()


async def test_unloaded_entry_scans_lan_at_next_setup(
    hass: HomeAssistant, popur: FakePopur
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="account-id")
    entry.add_to_hass(hass)
    assert (await discover(hass))["reason"] == "already_configured"

    popur.hass = hass
    popur.account.home_devices.return_value = [device()]

    def client(transport):
        popur.client.transport = transport
        return popur.client

    with (
        patch("custom_components.popur_s7.ThingMobileApi"),
        patch("custom_components.popur_s7.MobileAppProfile"),
        patch("custom_components.popur_s7.PopurAccount", return_value=popur.account),
        patch("custom_components.popur_s7.PopurClient", side_effect=client),
        patch("custom_components.popur_s7.LocalTuyaTransport") as transport,
        patch("custom_components.popur_s7.find_lan_hosts", return_value=[NEW_IP]) as scan,
    ):
        transport.return_value.connect = AsyncMock()
        transport.return_value.close = AsyncMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        scan.assert_awaited_once_with(MAC)
        transport.assert_called_once_with(NEW_IP, DEV_ID, "k", timeout=5.0)
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
        ConfigEntryState.NOT_LOADED,
    ],
)
async def test_dhcp_during_reload_is_not_discarded(
    hass: HomeAssistant, state: ConfigEntryState
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="account-id", state=state)
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_NETWORK_MAC, MAC)},
    )

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        async with entry.setup_lock:
            assert (await discover(hass))["reason"] == "already_configured"
    reload.assert_called_once_with(entry.entry_id)


async def test_dhcp_host_retained_when_scan_and_connect_fail(
    hass: HomeAssistant, popur: FakePopur
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="account-id")
    entry.add_to_hass(hass)
    popur.hass = hass
    popur.account.home_devices.return_value = [device()]
    hass.config.components.add("dhcp")

    def local(host, *_args, **_kwargs):
        return SimpleNamespace(
            config=SimpleNamespace(host=host),
            connect=AsyncMock(side_effect=TimeoutError),
            close=AsyncMock(),
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
        patch("custom_components.popur_s7.find_lan_hosts", return_value=[]) as scan,
        patch(
            "homeassistant.components.dhcp.async_discovered_service_info",
            return_value=[
                DhcpServiceInfo(ip=NEW_IP, macaddress=MAC.replace(":", ""), hostname="popur")
            ],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert isinstance(popur.client.transport, FallbackTransport)
        assert popur.client.transport.primary.config.host == NEW_IP
        scan.assert_awaited_once_with(MAC)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_new_dhcp_setup_does_not_save_host_override(hass: HomeAssistant) -> None:
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
    assert set(result["data"]) == {CONF_EMAIL, CONF_PASSWORD, CONF_INSTALL_ID, CONF_HOST}


async def test_other_registered_devices_do_not_start_setup(hass: HomeAssistant) -> None:
    with patch("custom_components.popur_s7.config_flow.asyncio.open_connection") as tcp:
        result = await discover(hass, mac="001122aabbcc")
    assert result["reason"] == "not_popur_device"
    tcp.assert_not_called()


async def test_reconfigure_can_clear_fixed_host(hass: HomeAssistant) -> None:
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
    reload.assert_awaited_once_with(entry.entry_id)
