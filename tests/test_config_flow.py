"""Pawfit setup/recovery through HA's actual flow manager."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from custom_components.pawfit.exceptions import (
    PawfitAuthenticationError,
    PawfitConnectionError,
    PawfitInvalidResponseError,
)
from .conftest import DOMAIN, MockConfigEntry


@pytest.mark.parametrize(
    "error,expected",
    [
        (PawfitAuthenticationError("safe"), "invalid_auth"),
        (PawfitConnectionError("safe"), "cannot_connect"),
        (PawfitInvalidResponseError("safe"), "invalid_response"),
    ],
)
async def test_login_errors_are_distinct_and_retryable(hass, pawfit, error, expected):
    with patch(
        "custom_components.pawfit.config_flow.PawfitConfigFlow._login",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"username": "a", "password": "b"},
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "a", "password": "new"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["data"] == {"username": "a", "password": "new", "userId": "u1"}
    assert result["result"].unique_id == "u1"


async def test_duplicate_legacy_account_is_not_created(hass, pawfit):
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "A", "password": "b"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"username": "a", "password": "b"},
    )
    assert result["type"] == "abort"
    assert result["reason"] in ("already_configured", "single_instance_allowed")


async def test_reauth_preserves_entry_and_rejects_different_account(hass, pawfit):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "a", "password": "old", "userId": "u1"}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    with patch(
        "custom_components.pawfit.config_flow.PawfitApiClient.async_login",
        return_value={"userId": "other"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "a", "password": "wrong-account"}
        )
    assert result["errors"] == {"base": "wrong_account"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "a", "password": "new"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_auth_failure_at_setup_starts_reauth(hass, pawfit):
    pawfit.reject_login = True
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "a", "password": "wrong", "userId": "u1"}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert any(
        flow["context"]["source"] == config_entries.SOURCE_REAUTH
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_setup_uses_the_login_accepted_by_the_config_flow(hass, pawfit):
    pawfit.login_limit = 1
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"username": "spare@example.com", "password": "fake-password"},
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert pawfit.count("login") == 1
    assert result["result"].state is config_entries.ConfigEntryState.LOADED
    assert pawfit.count("listpetinvitee") == 1
    assert "sessionId" not in result["result"].data


async def test_reauth_uses_the_new_session_without_logging_in_again(hass, pawfit):
    pawfit.login_limit = 1
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="u1",
        data={"username": "a", "password": "old", "userId": "u1"},
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"username": "a", "password": "new"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert pawfit.count("login") == 1
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.data == {"username": "a", "password": "new", "userId": "u1"}


async def test_user_form_and_real_login_do_not_store_session_token(hass, pawfit):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "a", "password": "b"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["data"] == {"username": "a", "password": "b", "userId": "u1"}
    assert pawfit.count("login") == 1


async def test_legacy_duplicate_check_catches_renamed_account_id(hass, pawfit):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="old-id", data={"username": "A", "password": "b"}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"username": "a", "password": "b"},
    )
    assert result["type"] == "abort"
    assert result["reason"] in ("already_configured", "single_instance_allowed")
