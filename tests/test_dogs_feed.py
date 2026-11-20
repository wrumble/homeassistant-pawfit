"""Exercise SofaPilot's read-only feed through Home Assistant's HTTP server."""

from datetime import datetime, timezone

import pytest
from homeassistant.components.http import config as http_config
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .conftest import advance

URL = "/api/sofapilot/dogs"
LAN = {"X-Forwarded-For": "192.168.3.12"}


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Prepare the synthetic HA Recorder database before HA starts."""


@pytest.fixture(autouse=True)
async def trusted_test_proxy(hass):
    assert await async_setup_component(
        hass,
        "http",
        {
            "http": {
                "use_x_forwarded_for": True,
                "trusted_proxies": ["127.0.0.1"],
            }
        },
    )
    if load_store := getattr(http_config, "async_get_and_load_store", None):
        store = await load_store(hass)
        await store.async_promote_pending()


async def test_dogs_feed_reports_observations_and_alerts(
    hass, pawfit, freezer, setup_entry, hass_client, hass_read_only_access_token
):
    await async_setup_component(hass, "http", {})
    epoch = int(datetime.now(timezone.utc).timestamp())
    pawfit.fix_times["1001"] = epoch
    pawfit.coordinates["1001"] = {"latitude": 0.0, "longitude": -1.25}
    pawfit.power["1001"] = 20
    await advance(hass, freezer, 61)
    client = await hass_client()
    response = await client.get(URL, headers=LAN)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = await response.json()
    assert len(payload["dogs"]) == 2
    dog = next(d for d in payload["dogs"] if d["name"] == "Tom")
    registry = er.async_get(hass)
    tracker = registry.async_get(
        registry.async_get_entity_id("device_tracker", "pawfit", "11")
    )
    assert (
        hass.states.get(tracker.entity_id).attributes["sofapilot_dog_id"] == tracker.id
    )
    stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    assert dog == {
        "id": tracker.id,
        "name": "Tom",
        "batteryPercent": 20,
        "charging": False,
        "fix": {
            "latitude": 0.0,
            "longitude": -1.25,
            "recordedAt": stamp,
            "accuracyMeters": 12,
            "retained": False,
            "stale": False,
        },
        "trail": [
            {
                "latitude": 0.0,
                "longitude": -1.25,
                "recordedAt": stamp,
                "breakBefore": True,
            }
        ],
    }
    assert [
        (a["id"], a["kind"]) for a in payload["alerts"] if a["dogId"] == tracker.id
    ] == [(tracker.id + ":lowBattery", "lowBattery")]
    no_auth = await client.get(URL, headers={**LAN, "Authorization": ""})
    assert no_auth.status == 401
    denied = await client.get(
        URL, headers={**LAN, "Authorization": f"Bearer {hass_read_only_access_token}"}
    )
    assert denied.status in {401, 403}
    for peer in ("8.8.8.8", "127.0.0.1", "::1", "169.254.1.2"):
        assert (await client.get(URL, headers={"X-Forwarded-For": peer})).status == 403
    # Direct test connections reach HA via loopback.
    assert (await client.get(URL)).status == 403
    for marker in ("?1", ""):
        assert (
            await client.get(URL, headers={**LAN, "Tailscale-Funnel-Request": marker})
        ).status == 403
    for peer in ("100.64.0.1", "fd7a:115c:a1e0::42"):
        assert (await client.get(URL, headers={"X-Forwarded-For": peer})).status == 200


async def test_recorder_restores_previous_walk_across_reload(
    hass, pawfit, freezer, recorder_mock, setup_entry, hass_client
):
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    client = await hass_client()
    start = int(datetime.now(timezone.utc).timestamp())
    pawfit.fix_times["1001"] = start
    await advance(hass, freezer, 61)
    pawfit.fix_times["1001"] = start + 60
    await advance(hass, freezer, 61)
    await async_wait_recording_done(hass)
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    pawfit.fix_times["1001"] = start + 122
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()
    response = await client.get(URL, headers=LAN)
    dog = next(d for d in (await response.json())["dogs"] if d["name"] == "Tom")
    assert [
        datetime.fromisoformat(p["recordedAt"]).timestamp() for p in dog["trail"]
    ] == [start, start + 60, start + 122]
    assert [p["breakBefore"] for p in dog["trail"]] == [True, False, True]


async def test_dog_trail_skips_repeats_breaks_at_outages_and_expires(
    hass, pawfit, freezer, setup_entry, hass_client, hass_access_token
):
    client = await hass_client()
    refresh = hass.auth.async_validate_access_token(hass_access_token)
    start = int(datetime.now(timezone.utc).timestamp())

    async def snapshot():
        token = hass.auth.async_create_access_token(refresh)
        response = await client.get(
            URL, headers={**LAN, "Authorization": f"Bearer {token}"}
        )
        payload = await response.json()
        dog = next(d for d in payload["dogs"] if d["name"] == "Tom")
        return dog, [a["kind"] for a in payload["alerts"] if a["dogId"] == dog["id"]]

    pawfit.fix_times["1001"] = start
    await advance(hass, freezer, 60, step=60)
    pawfit.coordinates["1001"] = {"latitude": 100}
    await advance(hass, freezer, 60, step=60)
    dog, alerts = await snapshot()
    assert dog["fix"]["retained"] and len(dog["trail"]) == 1 and not alerts
    pawfit.coordinates.clear()
    pawfit.fix_times["1001"] = start + 120
    await advance(hass, freezer, 60, step=60)
    await advance(hass, freezer, 60, step=60)
    assert [p["breakBefore"] for p in (await snapshot())[0]["trail"]] == [True, True]

    freezer.move_to(datetime.fromtimestamp(start + 120 + 900, timezone.utc))
    dog, alerts = await snapshot()
    assert dog["fix"]["stale"] and alerts == ["overdueFix"]
    pawfit.fix_times["1001"] = start + 1021
    await advance(hass, freezer, 60, step=60)
    dog, alerts = await snapshot()
    assert not dog["fix"]["stale"] and not alerts
    assert [p["breakBefore"] for p in dog["trail"]] == [True, True, True]
    freezer.tick(6 * 3600)
    assert (await snapshot())[0]["trail"] == []


async def test_disabled_or_lookalike_entities_are_never_returned(
    hass, setup_entry, hass_client
):
    client = await hass_client()
    registry = er.async_get(hass)
    tracker = registry.async_get_entity_id("device_tracker", "pawfit", "11")
    registry.async_update_entity(tracker, disabled_by=er.RegistryEntryDisabler.USER)
    hass.states.async_set(
        "device_tracker.other_dog", "home", {"sofapilot_dog_id": "other"}
    )
    response = await client.get(URL, headers=LAN)
    dogs = (await response.json())["dogs"]
    assert [dog["name"] for dog in dogs] == ["Ginger"]


async def test_six_hour_walk_never_exceeds_360_points(
    hass, pawfit, freezer, setup_entry, hass_client, hass_access_token
):
    client = await hass_client()
    refresh = hass.auth.async_validate_access_token(hass_access_token)
    start = int(datetime.now(timezone.utc).timestamp())
    for i in range(361):
        pawfit.fix_times["1001"] = start + 60 * (i + 1)
        await advance(hass, freezer, 60, step=60)
    token = hass.auth.async_create_access_token(refresh)
    response = await client.get(
        URL, headers={**LAN, "Authorization": f"Bearer {token}"}
    )
    dog = next(d for d in (await response.json())["dogs"] if d["name"] == "Tom")
    assert len(dog["trail"]) == 360
    first, last = dog["trail"][0], dog["trail"][-1]
    assert first["breakBefore"] and not last["breakBefore"]
    assert datetime.fromisoformat(first["recordedAt"]).timestamp() == start + 120
    assert datetime.fromisoformat(last["recordedAt"]).timestamp() == start + 361 * 60
