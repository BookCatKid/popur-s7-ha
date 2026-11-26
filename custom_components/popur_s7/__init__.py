"""The Popur integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.typing import ConfigType

from pypopur.client import PopurClient
from pypopur.discovery import find_lan_hosts
from pypopur.events import DeviceEvent
from pypopur.local import LocalTuyaTransport
from pypopur.mobile import (
    AccountDevice,
    MobileAppProfile,
    MobileAuthenticationError,
    PopurAccount,
    ThingMobileApi,
)
from pypopur.transport import FallbackTransport, PopurTransport

from .const import (
    CONF_DISCOVERED_HOSTS,
    CONF_HOST,
    CONF_INSTALL_ID,
    DOMAIN,
    LOCAL_REFRESH_INTERVAL,
)
from .coordinator import PopurCoordinator, async_close_events
from .transport import CloudHttpTransport

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class PopurEntryData:
    """Runtime state for one config entry."""

    account: PopurAccount
    home_id: int | str
    coordinator: PopurCoordinator
    account_devices: list[AccountDevice] = field(default_factory=list)
    clients: dict[str, PopurClient] = field(default_factory=dict)
    events: Any = None


PopurConfigEntry = ConfigEntry[PopurEntryData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def _local_transport(
    device: AccountDevice,
    host_override: str | None,
    discovered_host: str | None = None,
    *,
    announced_host: str | None = None,
) -> LocalTuyaTransport | None:
    """Build the LAN transport for one device — explicit host first, then
    ARP/neigh by the cloud-recorded MAC, then a port-6668 subnet probe.

    Candidates are connect-verified (the handshake checks local_key +
    devId), but a transport is returned even when every probe fails: the
    S7 accepts a single LAN session at a time, so a momentarily-busy
    device must stay retryable via FallbackTransport rather than pin the
    entry to the cloud until the next reload."""

    if not device.local_key:
        return None
    first: LocalTuyaTransport | None = None

    async def try_host(host: str, *, remember: bool = True) -> LocalTuyaTransport | None:
        nonlocal first
        transport = LocalTuyaTransport(
            host, device.device_id, device.local_key, timeout=5.0
        )
        if remember and first is None:
            first = transport
        try:
            await transport.connect()
            if not remember and transport.protocol_version == "3.3":
                await transport.close()
                return None
        except Exception as err:  # noqa: BLE001 — try the next candidate
            _LOGGER.debug("local connect to %s failed: %s", host, err)
            return None
        _LOGGER.info(
            "Popur %s local channel up at %s (pv %s)",
            device.device_id,
            host,
            transport.protocol_version,
        )
        return transport

    checked: set[str] = set()
    if not host_override and announced_host and announced_host != discovered_host:
        checked.add(announced_host)
        if connected := await try_host(announced_host, remember=False):
            return connected
    if preferred := host_override or discovered_host:
        checked.add(preferred)
        if connected := await try_host(preferred):
            return connected

    # Try discovery once at setup if a remembered DHCP address has gone stale.
    if not host_override:
        try:
            candidates = await find_lan_hosts(device.mac)
        except Exception:
            _LOGGER.debug("LAN host scan failed", exc_info=True)
        else:
            for host in candidates:
                if host not in checked and (connected := await try_host(host)):
                    return connected

    if first is not None:
        _LOGGER.info(
            "Popur %s LAN unreachable at setup — will keep retrying %s",
            device.device_id,
            first.config.host,
        )
    return first


async def async_setup_entry(hass: HomeAssistant, entry: PopurConfigEntry) -> bool:
    """Log in, enumerate devices, and start the coordinator."""
    install_id = entry.data[CONF_INSTALL_ID]
    api = ThingMobileApi(
        MobileAppProfile.bundled_popur_app2(), install_id=install_id
    )
    account = PopurAccount(api)
    try:
        await account.login(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    except MobileAuthenticationError as err:
        raise ConfigEntryAuthFailed("Popur credentials rejected") from err
    except Exception as err:
        raise ConfigEntryNotReady(f"Popur login failed: {err}") from err

    try:
        homes = await account.homes()
    except Exception as err:
        raise ConfigEntryNotReady(f"could not list homes: {err}") from err
    if not homes:
        raise ConfigEntryNotReady("account has no homes")

    home_id: int | str = homes[0]["gid"]
    try:
        devices = await account.home_devices(home_id)
    except Exception as err:
        raise ConfigEntryNotReady(f"could not list devices: {err}") from err

    # The optional host override only makes sense for a single-device home.
    host_override = entry.data.get(CONF_HOST) or None
    if host_override and len(devices) != 1:
        _LOGGER.warning(
            "ignoring configured host %s with %d devices — use discovery",
            host_override,
            len(devices),
        )
        host_override = None

    discovered_hosts = entry.data.get(CONF_DISCOVERED_HOSTS, {})
    announced_hosts: dict[str, str] = {}
    if "dhcp" in hass.config.components:
        from homeassistant.components.dhcp import async_discovered_service_info

        # Includes announcements HA observed before or during this entry's setup.
        announced_hosts.update(
            (format_mac(info.macaddress), info.ip) for info in async_discovered_service_info(hass)
        )
    clients: dict[str, PopurClient] = {}
    for device in devices:
        cloud: PopurTransport = CloudHttpTransport(account, device.device_id)
        mac = format_mac(device.mac) if device.mac else None
        local = await _local_transport(
            device, host_override, discovered_hosts.get(mac),
            announced_host=announced_hosts.get(mac) if mac else None,
        )
        transport: PopurTransport = (
            FallbackTransport(local, cloud) if local is not None else cloud
        )
        clients[device.device_id] = PopurClient(transport)

    coordinator = PopurCoordinator(
        hass,
        account,
        home_id,
        clients,
        LOCAL_REFRESH_INTERVAL,
    )
    await coordinator.async_config_entry_first_refresh()

    # Static device metadata for the registry + diagnostic sensors.
    coordinator.install_id = entry.data.get(CONF_INSTALL_ID, "")
    for device in devices:
        try:
            modules = await account.firmware_info(device.device_id, home_id=home_id)
            coordinator.firmware_versions[device.device_id] = ", ".join(
                f"{m.type_desc} {m.current_version}" if m.type_desc else m.current_version
                for m in modules
                if m.current_version
            ) or None
        except Exception:
            _LOGGER.debug(
                "firmware info failed for %s", device.device_id, exc_info=True
            )

    entry_data = PopurEntryData(
        account=account,
        home_id=home_id,
        coordinator=coordinator,
        account_devices=list(devices),
        clients=clients,
    )
    entry.runtime_data = entry_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Real-time push: decode MQTT frames into coordinator merges, and
    # resync state on every (re)connect. Connect only once the platforms
    # are set up, so event entities already listen for the first push.
    # The wire client is thread-based; hop onto the loop for state updates.
    try:
        local_keys = {
            dev.device_id: dev.local_key
            for dev in devices
            if dev.local_key
        }

        def _on_event(event: DeviceEvent) -> None:
            if event.dps:
                hass.loop.call_soon_threadsafe(
                    coordinator.push_dps, event.dev_id, dict(event.dps)
                )

        def _on_error(topic: str, code: str, message: str) -> None:
            # Undecodable frames, dedup repeats and connect errors: without
            # this they are dropped silently.
            _LOGGER.debug("Popur MQTT error on %s: %s %s", topic, code, message)

        entry_data.events = account.connect_events(
            devices=local_keys,
            on_event=_on_event,
            on_error=_on_error,
            on_connect=coordinator.request_refresh,
        )
    except Exception as err:  # noqa: BLE001 — realtime is best-effort; polling still works
        _LOGGER.warning("Popur realtime events unavailable, polling only: %s", err)
        return True

    # Hand the session to the coordinator even if the initial connect fails.
    # The coordinator only retries that first handshake; after a successful
    # connection pypopur owns subsequent link-loss recovery.
    coordinator.events = entry_data.events
    try:
        await entry_data.events.connect()
    except Exception as err:  # noqa: BLE001 — retried from the polling tick
        coordinator._needs_initial_mqtt_reconnect = True
        _LOGGER.warning("Popur realtime events not connected yet, retrying: %s", err)
    else:
        _LOGGER.debug("Popur MQTT events connected")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PopurConfigEntry) -> bool:
    entry.runtime_data.coordinator.cancel_record_fetch()
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = entry.runtime_data
        # Detach the session from the coordinator first: a refresh still
        # running then cannot start a reconnect, and a reconnect already in
        # its handshake closes the session itself once it sees this.
        data.coordinator.events = None
        await data.coordinator.async_wait_mqtt_reconnect()
        if data.events is not None:
            await async_close_events(data.events)
        for client in data.clients.values():
            await client.close()
    return unload_ok
