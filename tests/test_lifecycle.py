"""Lifecycle regressions exercised without real accounts or tracker commands."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.pawfit import async_setup_entry, async_unload_entry
from custom_components.pawfit.exceptions import PawfitConnectionError
from custom_components.pawfit.pawfit_api import PawfitApiClient
from custom_components.pawfit.session_handoff import (
    stage_authenticated_client,
    take_authenticated_client,
)
from .conftest import DOMAIN, MockConfigEntry, advance


@pytest.mark.parametrize("result", [[], PawfitConnectionError("private error")])
async def test_account_unavailable_can_retry_setup(hass, result):
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "a", "password": "b"})
    client = AsyncMock()
    if isinstance(result, Exception):
        client.async_get_trackers.side_effect = result
    else:
        client.async_get_trackers.return_value = result
    with patch("custom_components.pawfit.PawfitApiClient", return_value=client):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
    assert not hass.data.get(DOMAIN)


async def test_platform_failure_shuts_down_coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "a", "password": "b"})
    client = AsyncMock()
    client.async_get_trackers.return_value = [{"tracker_id": "1001"}]
    coordinator = AsyncMock()
    with (
        patch("custom_components.pawfit.PawfitApiClient", return_value=client),
        patch(
            "custom_components.pawfit.PawfitDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("platform failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="platform failed"):
            await async_setup_entry(hass, entry)
    coordinator.async_shutdown.assert_awaited_once()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_failed_unload_keeps_working_coordinator(hass, setup_entry):
    coordinator = hass.data[DOMAIN][setup_entry.entry_id]
    with (
        patch.object(hass.config_entries, "async_unload_platforms", return_value=False),
        patch.object(coordinator, "async_shutdown") as shutdown,
    ):
        assert not await async_unload_entry(hass, setup_entry)
    shutdown.assert_not_called()
    assert hass.data[DOMAIN][setup_entry.entry_id] is coordinator


async def test_handoff_is_short_lived_and_bound_to_one_login(hass, pawfit, freezer):
    client = PawfitApiClient("a", "b", async_get_clientsession(hass))
    await client.async_login()
    entry = {"userId": "u1", "username": "a", "password": "b"}
    stage_authenticated_client(hass, "u1", client)

    assert take_authenticated_client(hass, {**entry, "userId": "u2"}) is None
    assert take_authenticated_client(hass, {**entry, "username": "other"}) is None
    assert take_authenticated_client(hass, {**entry, "password": "changed"}) is None
    assert take_authenticated_client(hass, entry) is client
    assert take_authenticated_client(hass, entry) is None

    stage_authenticated_client(hass, "u1", client)
    await advance(hass, freezer, 20)
    stage_authenticated_client(hass, "u1", client)
    await advance(hass, freezer, 20)
    assert take_authenticated_client(hass, entry) is client

    stage_authenticated_client(hass, "u1", client)
    await advance(hass, freezer, 31)
    assert take_authenticated_client(hass, entry) is None
