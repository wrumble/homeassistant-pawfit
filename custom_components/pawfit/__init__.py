"""The Pawfit integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .pawfit_api import PawfitApiClient
from .device_tracker import PawfitDataUpdateCoordinator
from .const import DOMAIN
from .dogs_feed import DogsView
from .exceptions import PawfitAuthenticationError, PawfitError
from .session_handoff import take_authenticated_client


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pawfit from a config entry."""
    # Create the API client. Fork change: HA's shared HTTP session (upstream
    # opened a new one on every setup and never closed it), and HA's clock
    # for the daily request count.
    client = take_authenticated_client(hass, entry.data)
    if client is None:
        client = PawfitApiClient(
            entry.data["username"],
            entry.data["password"],
            async_get_clientsession(hass),
            now_fn=dt_util.now,
        )

    # Get trackers and create coordinator
    try:
        trackers = await client.async_get_trackers()
    except PawfitAuthenticationError as error:
        _LOGGER.warning("Pawfit setup login refused: %s", error)
        raise ConfigEntryAuthFailed("Pawfit credentials were rejected") from None
    except PawfitError:
        raise ConfigEntryNotReady("Pawfit account data is unavailable") from None
    if not trackers:
        raise ConfigEntryNotReady("No shared trackers were returned by Pawfit")
    coordinator = PawfitDataUpdateCoordinator(hass, client, trackers)

    # Start the coordinator to begin polling
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator in hass.data for platforms to use
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Forward the config entry setup to multiple platforms
    try:
        await hass.config_entries.async_forward_entry_setups(
            entry, ["device_tracker", "binary_sensor", "sensor", "button"]
        )
    except Exception:
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, ["device_tracker", "binary_sensor", "sensor", "button"]
    )
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the read-only SofaPilot view once per HA instance."""
    hass.http.register_view(DogsView(hass))
    return True
