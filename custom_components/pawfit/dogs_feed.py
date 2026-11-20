"""Admin-only SofaPilot read model of the enabled Pawfit trackers."""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import ip_address, ip_network

from aiohttp import web
from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import LOCATE_CLOCK_SLACK_SECONDS, STALE_AFTER_SECONDS
from .helpers import battery_level, charging, coordinate

_LOGGER = logging.getLogger(__name__)
DOGS = "pawfit_sofapilot_dogs"
MAX_DOGS = 32
MAX_POINTS = 360
TRAIL_WINDOW = timedelta(hours=6)
NETWORKS = tuple(
    ip_network(prefix)
    for prefix in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fd7a:115c:a1e0::/48",
    )
)


@dataclass(frozen=True)
class Position:
    latitude: float
    longitude: float
    time: datetime
    accuracy: float | None

    def coordinates(self):
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "recordedAt": self.time.isoformat(),
        }


class Dog:
    def __init__(self, registry_id, name, entity_id):
        self.id = registry_id
        self.name = str(name)[:128]
        self.entity_id = entity_id
        self.started = dt_util.utcnow()
        self.fix = None
        self.battery = self.charging = None
        self.retained = False
        self.break_pending = True
        self.points = deque(maxlen=MAX_POINTS)
        self.prior_points = []
        self.history_loaded = False

    def observe(self, data, available):
        model = (data.get("_raw") or {}).get("model")
        self.battery = battery_level(data.get("battery"), model) if available else None
        self.charging = charging(data.get("battery"), model) if available else None
        cached = data.get("data_cached") or data.get("position_cached")
        stamp = data.get("position_fix_time")
        if stamp is None:
            self.break_pending = True
            self.retained = self.fix is not None
            return
        position = Position(
            data["latitude"],
            data["longitude"],
            dt_util.utc_from_timestamp(stamp),
            data["accuracy"],
        )
        if not available or cached:
            if self.fix is None or self.fix.time < position.time:
                self.fix = position
            self.break_pending = self.retained = True
            return
        self.retained = False
        if self.fix and position.time <= self.fix.time:
            if position.time < self.fix.time:
                self.retained = self.break_pending = True
            return
        gap = (
            self.fix is None
            or (position.time - self.fix.time).total_seconds() > STALE_AFTER_SECONDS
        )
        self.points.append((position, self.break_pending or gap))
        self.fix = position
        self.break_pending = False

    def snapshot(self, now):
        fix = self.fix
        overdue = (
            now - (fix.time if fix else self.started)
        ).total_seconds() >= STALE_AFTER_SECONDS
        cutoff = now - TRAIL_WINDOW
        points = [p for p in self.prior_points if p[0].time >= cutoff]
        last_prior = points[-1][0].time if points else None
        live = [
            p
            for p in self.points
            if p[0].time >= cutoff and (last_prior is None or p[0].time > last_prior)
        ]
        if points and live:
            live[0] = (live[0][0], True)  # Discontinuity across an HA reload.
        points.extend(live)
        trail = [
            {**point.coordinates(), "breakBefore": index == 0 or gap}
            for index, (point, gap) in enumerate(points[-MAX_POINTS:])
        ]
        dog = {
            "id": self.id,
            "name": self.name,
            "fix": {
                **fix.coordinates(),
                "accuracyMeters": fix.accuracy,
                "stale": overdue,
                "retained": self.retained,
            }
            if fix
            else None,
            "batteryPercent": self.battery,
            "charging": self.charging,
            "trail": trail,
        }
        alerts = []
        if self.battery is not None and self.battery <= 20:
            alerts.append(
                self.alert(
                    "lowBattery", f"{self.name}'s collar battery is at {self.battery}%."
                )
            )
        if overdue:
            alerts.append(
                self.alert(
                    "overdueFix",
                    f"{self.name} has no GPS fix from the last 15 minutes.",
                )
            )
        return dog, alerts

    def alert(self, kind, message):
        return {
            "id": f"{self.id}:{kind}",
            "dogId": self.id,
            "kind": kind,
            "message": message,
        }


def register_dog(entity, name):
    """Register only an entity HA actually added; dispose its trail on removal."""
    registry = entity.registry_entry
    if not registry or registry.disabled:
        return None
    dogs = entity.hass.data.setdefault(DOGS, {})
    if len(dogs) >= MAX_DOGS:
        return None
    dog = Dog(registry.id, name, entity.entity_id)
    dogs[dog.id] = dog
    entity.async_on_remove(lambda: dogs.pop(dog.id, None))
    return dog


class DogsView(HomeAssistantView):
    url = "/api/sofapilot/dogs"
    name = "api:sofapilot:dogs"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self.lock = asyncio.Lock()
        self.retry_after = None

    def enabled(self):
        registry = er.async_get(self.hass)
        for dog in self.hass.data.get(DOGS, {}).values():
            entry = registry.async_get(dog.entity_id)
            if entry and entry.id == dog.id and not entry.disabled:
                yield dog

    @require_admin
    async def get(self, request):
        if "Tailscale-Funnel-Request" in request.headers:
            raise web.HTTPForbidden()
        # Only the peer resolved by HA's trusted-proxy middleware is authoritative.
        try:
            remote = ip_address(request.remote)
        except ValueError:
            raise web.HTTPForbidden() from None
        if not any(remote in network for network in NETWORKS):
            raise web.HTTPForbidden()
        async with self.lock:
            await self.restore_history()
            dogs, alerts = [], []
            now = dt_util.utcnow()
            for dog in self.enabled():
                snapshot, active = dog.snapshot(now)
                dogs.append(snapshot)
                alerts.extend(active)
            return self.json(
                {"dogs": dogs, "alerts": alerts}, headers={"Cache-Control": "no-store"}
            )

    async def restore_history(self):
        pending = [dog for dog in self.enabled() if not dog.history_loaded]
        if not pending or "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance
        from sqlalchemy.exc import SQLAlchemyError

        now = dt_util.utcnow()
        if self.retry_after and self.retry_after > now:
            return
        recorder = get_instance(self.hass)
        for dog in pending:
            try:
                points = await recorder.async_add_executor_job(
                    read_recorded_walk,
                    self.hass,
                    dog.entity_id,
                    dog.id,
                    dog.started,
                )
            except SQLAlchemyError:
                self.retry_after = now + timedelta(minutes=1)
                _LOGGER.warning("SofaPilot walk history temporarily unavailable")
                return
            dog.prior_points = points
            if points and (not dog.fix or points[-1][0].time > dog.fix.time):
                dog.fix = points[-1][0]
                dog.retained = dog.break_pending = True
            dog.history_loaded = True


def read_recorded_walk(hass, entity_id, registry_id, end):
    """Read at most 720 rows, including attribute-only fixes within the same zone."""
    from json import JSONDecodeError, loads

    from homeassistant.components.recorder.db_schema import (
        StateAttributes,
        States,
        StatesMeta,
    )
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import select

    cutoff = end - TRAIL_WINDOW
    # History's general significant-state API has no row limit. Apply the limit in
    # the DB so rapid HA state writes cannot cause an unbounded result allocation.
    query = (
        select(States.state, States.last_updated_ts, StateAttributes.shared_attrs)
        .select_from(States)
        .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
        .outerjoin(
            StateAttributes, States.attributes_id == StateAttributes.attributes_id
        )
        .where(
            StatesMeta.entity_id == entity_id,
            States.last_updated_ts >= cutoff.timestamp(),
            States.last_updated_ts < end.timestamp(),
        )
        .order_by(States.last_updated_ts.desc(), States.state_id.desc())
        .limit(MAX_POINTS * 2)
    )
    with session_scope(hass=hass, read_only=True) as session:
        rows = session.execute(query).all()
    points = deque(maxlen=MAX_POINTS)
    gap = True
    for state, observed, serialized in reversed(rows):
        try:
            attrs = loads(serialized) if serialized else {}
        except (TypeError, JSONDecodeError):
            attrs = None
        if (
            not isinstance(attrs, dict)
            or attrs.get("sofapilot_dog_id") != registry_id
            or state in ("unavailable", "unknown")
            or attrs.get("position_cached")
            or attrs.get("data_cached")
        ):
            gap = True
            continue
        lat = coordinate(attrs.get("latitude"), -90, 90)
        lon = coordinate(attrs.get("longitude"), -180, 180)
        raw_time = attrs.get("last_seen")
        try:
            stamp = (
                dt_util.parse_datetime(raw_time) if isinstance(raw_time, str) else None
            )
        except (ValueError, OverflowError):
            stamp = None
        if (
            lat is None
            or lon is None
            or stamp is None
            or stamp.tzinfo is None
            or stamp < cutoff
            or stamp.timestamp() > observed + LOCATE_CLOCK_SLACK_SECONDS
        ):
            gap = True
            continue
        if points:
            delta = (stamp - points[-1][0].time).total_seconds()
            if delta <= 0:
                gap |= delta < 0
                continue
            gap |= delta > STALE_AFTER_SECONDS
        point = Position(
            lat,
            lon,
            dt_util.as_utc(stamp),
            coordinate(attrs.get("gps_accuracy"), 0, 10_000_000),
        )
        points.append((point, gap))
        gap = False
    return list(points)
