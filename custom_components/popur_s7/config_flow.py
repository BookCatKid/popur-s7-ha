"""Config flow for Popur."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from pypopur.mobile import (
    MobileAppProfile,
    MobileAuthenticationError,
    PopurAccount,
    ThingMobileApi,
)

from .const import (
    CONF_DISCOVERED_HOSTS,
    CONF_HOST,
    CONF_INSTALL_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_HOST): cv.string,
        vol.Optional(CONF_INSTALL_ID): cv.string,
    }
)


class PopurConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Popur config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_hosts: dict[str, str] = {}

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Use DHCP to discover devices and update known LAN addresses."""
        mac = format_mac(discovery_info.macaddress)
        entries = self._async_current_entries()
        if entries:
            for entry in entries:
                if entry.state is not ConfigEntryState.LOADED:
                    continue
                devices = entry.runtime_data.account_devices
                if (len(devices) == 1 and entry.data.get(CONF_HOST)) or not any(
                    device.local_key and device.mac and format_mac(device.mac) == mac
                    for device in devices
                ):
                    continue
                hosts = dict(entry.data.get(CONF_DISCOVERED_HOSTS, {}))
                if hosts.get(mac) != discovery_info.ip:
                    hosts[mac] = discovery_info.ip
                    if self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_DISCOVERED_HOSTS: hosts}
                    ):
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        # The OUI is not guaranteed Popur-exclusive, so confirm the Tuya LAN
        # port answers before surfacing the device as discovered.
        try:
            async with asyncio.timeout(2):
                reader, writer = await asyncio.open_connection(
                    discovery_info.ip, 6668
                )
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError):
            return self.async_abort(reason="not_popur_device")

        self._discovered_hosts[mac] = discovery_info.ip
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            install_id = user_input.get(CONF_INSTALL_ID) or uuid.uuid4().hex

            api = ThingMobileApi(
                MobileAppProfile.bundled_popur_app2(), install_id=install_id
            )
            account = PopurAccount(api)
            try:
                session = await account.login(email, password)
            except MobileAuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Popur login failed")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(session.uid or email)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_INSTALL_ID: install_id,
                        CONF_HOST: (user_input.get(CONF_HOST) or "").strip(),
                        CONF_DISCOVERED_HOSTS: self._discovered_hosts,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when the stored credentials are rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a new password for the existing Popur account."""
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            api = ThingMobileApi(
                MobileAppProfile.bundled_popur_app2(),
                install_id=entry.data[CONF_INSTALL_ID],
            )
            account = PopurAccount(api)
            try:
                session = await account.login(email, password)
            except MobileAuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Popur login failed")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(session.uid or email)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): cv.string}),
            description_placeholders={"email": email},
            errors=errors,
        )
