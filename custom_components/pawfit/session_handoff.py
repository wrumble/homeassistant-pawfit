"""Pass the form's verified Pawfit client to the imminent config entry setup."""

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

_CACHE_KEY = "pawfit_pending_authenticated_clients"
_HANDOFF_SECONDS = 30


@callback
def stage_authenticated_client(hass, account_id, client):
    """Keep the verified client in memory briefly, keyed by its Pawfit account."""
    pending = hass.data.setdefault(_CACHE_KEY, {})
    previous = pending.pop(account_id, None)
    if previous is not None:
        previous[1]()

    @callback
    def expire(_now):
        pending.pop(account_id, None)

    pending[account_id] = (
        client,
        async_call_later(hass, _HANDOFF_SECONDS, expire),
    )


@callback
def take_authenticated_client(hass, entry_data):
    """Consume the client once, only for the same account and credentials."""
    pending = hass.data.get(_CACHE_KEY, {})
    account_id = entry_data.get("userId")
    handoff = pending.get(account_id)
    if handoff is None:
        return None
    client, cancel_expiry = handoff
    if not client.matches_authenticated_account(
        account_id, entry_data["username"], entry_data["password"]
    ):
        return None
    del pending[account_id]
    cancel_expiry()
    return client
