"""Test the Popur reauthentication flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.popur_s7.const import CONF_HOST, CONF_INSTALL_ID, DOMAIN
from pypopur.mobile import MobileAuthenticationError

EMAIL = "test@example.com"
UID = "account-id"
DATA = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: "old-password",
    CONF_INSTALL_ID: "existing-install",
    CONF_HOST: "192.0.2.1",
}


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=UID, title=EMAIL)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def login():
    with (
        patch("custom_components.popur_s7.config_flow.MobileAppProfile"),
        patch("custom_components.popur_s7.config_flow.ThingMobileApi") as api,
        patch("custom_components.popur_s7.config_flow.PopurAccount") as account,
    ):
        account.return_value.login = AsyncMock(return_value=SimpleNamespace(uid=UID))
        yield account.return_value.login, api


async def start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == EMAIL
    assert set(result["data_schema"]({CONF_PASSWORD: "new-password"})) == {CONF_PASSWORD}
    return result


async def test_reauth_success(hass: HomeAssistant, entry: MockConfigEntry, login):
    authenticate, api = login
    result = await start_reauth(hass, entry)
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    authenticate.assert_awaited_once_with(EMAIL, "new-password")
    assert api.call_args.kwargs["install_id"] == DATA[CONF_INSTALL_ID]
    reload.assert_awaited_once_with(entry.entry_id)
    assert entry.data == {**DATA, CONF_PASSWORD: "new-password"}
    assert entry.unique_id == UID
    assert entry.title == EMAIL
    assert hass.config_entries.async_entries(DOMAIN) == [entry]


@pytest.mark.parametrize(
    ("exception", "error"),
    [(MobileAuthenticationError("rejected"), "invalid_auth"), (OSError("offline"), "cannot_connect")],
)
async def test_reauth_error(hass: HomeAssistant, entry: MockConfigEntry, login, exception, error):
    authenticate, _ = login
    authenticate.side_effect = exception
    result = await start_reauth(hass, entry)
    with patch.object(hass.config_entries, "async_reload") as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong-password"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": error}
        assert entry.data == DATA
        reload.assert_not_called()

        authenticate.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    reload.assert_awaited_once_with(entry.entry_id)


async def test_reauth_account_mismatch(hass: HomeAssistant, entry: MockConfigEntry, login):
    authenticate, _ = login
    authenticate.return_value = SimpleNamespace(uid="different-account")
    result = await start_reauth(hass, entry)
    with patch.object(hass.config_entries, "async_reload") as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data == DATA
    reload.assert_not_called()
