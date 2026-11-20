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
        # Keep all 361 device times inside the cutoff despite scheduler sub-seconds.
        pawfit.fix_times["1001"] = start + 60 * (i + 1) + 2
        await advance(hass, freezer, 60, step=60)
    token = hass.auth.async_create_access_token(refresh)
    response = await client.get(
        URL, headers={**LAN, "Authorization": f"Bearer {token}"}
    )
    dog = next(d for d in (await response.json())["dogs"] if d["name"] == "Tom")
    assert len(dog["trail"]) == 360
    first, last = dog["trail"][0], dog["trail"][-1]
    assert first["breakBefore"] and not last["breakBefore"]
    assert datetime.fromisoformat(first["recordedAt"]).timestamp() == start + 122
    assert datetime.fromisoformat(last["recordedAt"]).timestamp() == start + 361 * 60 + 2


@pytest.mark.parametrize("remote", [None, "invalid address"])
async def test_invalid_effective_peer_denied(
    hass, setup_entry, hass_admin_user, remote
):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from custom_components.pawfit.dogs_feed import DogsView

    request = make_mocked_request("GET", URL, headers=LAN).clone(remote=remote)
    request["hass_user"] = hass_admin_user
    with pytest.raises(web.HTTPForbidden):
        await DogsView(hass).get(request)


@pytest.fixture
def large_account(pawfit, monkeypatch):
    from .conftest import TRACKERS

    for i in range(33):
        tid = str(2000 + i)
        monkeypatch.setitem(TRACKERS, tid, {"name": f"Dog {i}", "petId": i + 30})
        pawfit.fix_times[tid] = int(datetime.now(timezone.utc).timestamp())
        pawfit.finding[tid] = False
        pawfit.timers[tid] = {}


async def test_registration_bounds_and_missing_identity(
    hass, large_account, setup_entry, hass_client, monkeypatch
):
    from types import SimpleNamespace

    from custom_components.pawfit.dogs_feed import register_dog

    # Transient/unmanaged tracker without an established HA identity.
    assert (
        register_dog(SimpleNamespace(hass=hass, registry_entry=None), "Unknown") is None
    )
    client = await hass_client()
    response = await client.get(URL, headers=LAN)
    dogs = (await response.json())["dogs"]
    assert len(dogs) == len({d["id"] for d in dogs}) == 32

    registry = er.async_get(hass)
    original = registry.async_get
    entity_id = registry.async_get_entity_id("device_tracker", "pawfit", "11")
    # Registry removal can precede the entity platform's queued unloading callback.
    monkeypatch.setattr(
        type(registry),
        "async_get",
        lambda self, key: None if key == entity_id else original(key),
    )
    response = await client.get(URL, headers=LAN)
    remaining = (await response.json())["dogs"]
    assert len(remaining) == 31 and all(d["name"] != "Tom" for d in remaining)


async def test_retained_coordinator_fix_when_entity_joins_mid_outage(
    hass, pawfit, setup_entry, hass_client
):
    from custom_components.pawfit.device_tracker import PawfitDeviceTracker
    from custom_components.pawfit.dogs_feed import register_dog

    coordinator = hass.data["pawfit"][setup_entry.entry_id]
    tracker = PawfitDeviceTracker(coordinator.trackers[0], coordinator)
    tracker.hass = hass
    registry = er.async_get(hass)
    tracker.entity_id = registry.async_get_entity_id("device_tracker", "pawfit", "11")
    tracker.registry_entry = registry.async_get(tracker.entity_id)
    dog = register_dog(tracker, "Tom")
    dog.observe({**coordinator.data["1001"], "data_cached": True}, True)
    client = await hass_client()
    payload = await (await client.get(URL, headers=LAN)).json()
    tom = next(d for d in payload["dogs"] if d["name"] == "Tom")
    assert tom["fix"]["retained"] and tom["trail"] == []


async def test_recorder_outage_retry_and_unusable_samples(
    hass, pawfit, freezer, recorder_mock, setup_entry, hass_client, hass_admin_user
):
    from homeassistant.components.recorder.db_schema import StateAttributes
    from homeassistant.components.recorder.util import session_scope
    from homeassistant.core import Context
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )
    from sqlalchemy import event, update
    from sqlalchemy.exc import OperationalError

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("device_tracker", "pawfit", "11")
    dog_id = registry.async_get(entity_id).id
    start = datetime.now(timezone.utc).replace(microsecond=0)
    await hass.config_entries.async_unload(setup_entry.entry_id)
    from datetime import timedelta

    def emit(offset, *, fix=None, user=None, **attrs):
        freezer.move_to(start + timedelta(seconds=offset))
        stamp = start + timedelta(seconds=offset if fix is None else fix)
        hass.states.async_set(
            entity_id,
            "not_home",
            {
                "latitude": 10,
                "longitude": 20,
                "gps_accuracy": 10,
                "last_seen": stamp.isoformat(),
                "sofapilot_dog_id": dog_id,
                **attrs,
            },
            context=Context(user_id=user),
        )

    emit(1)
    emit(2, fix=1, gps_accuracy=12)
    emit(3, sofapilot_dog_id="different")
    emit(4, latitude=95)
    emit(5, last_seen="2026-02-31T10:00:00+00:00")
    emit(6, marker="corrupt-me")
    emit(7)
    emit(8, user=hass_admin_user.id)
    await async_wait_recording_done(hass)

    def corrupt_one_row():
        with session_scope(hass=hass) as session:
            result = session.execute(
                update(StateAttributes)
                .where(StateAttributes.shared_attrs.like("%corrupt-me%"))
                .values(shared_attrs="broken-json")
            )
            assert result.rowcount == 1
            session.commit()

    await recorder_mock.async_add_executor_job(corrupt_one_row)
    hass.states.async_remove(entity_id)
    freezer.tick(1)
    # The collar restarts with regressed GPS; history still has the newer fix.
    pawfit.fix_times["1001"] = start.timestamp()
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client()

    async def snapshot():
        payload = await (await client.get(URL, headers=LAN)).json()
        return next(d for d in payload["dogs"] if d["id"] == dog_id)

    def unavailable(conn, cursor, statement, params, context, many):
        if (
            "states_meta.entity_id =" in statement
            and "state_attributes.shared_attrs" in statement
        ):
            raise OperationalError("synthetic recorder outage", {}, ConnectionError())

    event.listen(recorder_mock.engine, "before_cursor_execute", unavailable)
    try:
        assert not (await snapshot())["fix"]["retained"]
    finally:
        event.remove(recorder_mock.engine, "before_cursor_execute", unavailable)
    assert not (await snapshot())["fix"]["retained"]  # one-minute retry backoff
    freezer.tick(61)
    dog = await snapshot()
    assert dog["fix"]["retained"]
    assert [p["recordedAt"] for p in dog["trail"]] == [
        (start + timedelta(seconds=i)).isoformat() for i in (1, 7)
    ]
    assert [p["breakBefore"] for p in dog["trail"]] == [True, True]
    await advance(hass, freezer, 60, step=60)
    assert (await snapshot())["fix"]["recordedAt"] == (
        start + timedelta(seconds=7)
    ).isoformat()
