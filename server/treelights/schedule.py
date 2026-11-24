"""Timers: switch the lights on or off at set times, or relative to sunrise and sunset.

- Times are local to the configured time zone (or $TZ, else UTC). Daylight saving is
  handled: a timer at 18:00 fires at 18:00 local time on both sides of a change. A time
  that doesn't exist on the day the clocks go forward fires an hour later; a time that
  occurs twice fires once.
- Sunrise and sunset come from the configured location (`sun.py`). Where the sun doesn't
  rise or set that day (polar day or night), that day's sun timer doesn't fire.
- When several events fall in one check (the server was busy or the clock jumped), only the
  last one is applied: it decides the state the lights should be in.
- After a restart, the most recent event since the lights were last switched (at most a day
  ago) is applied, so a timer missed while the server was down still takes effect.
- A timer that fires while a scan holds the LEDs is skipped (`lights.LightsBusy`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import ConfigStore, ScheduleSettings, Timer
from .effects import LIBRARY
from .lights import Lights, LightsBusy
from .sun import sun_event

log = logging.getLogger(__name__)

CHECK_S = 30.0  # longest sleep between checks (config changes wake it at once)
CATCH_UP = timedelta(hours=24)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def resolve_tz(name: str | None) -> tuple[tzinfo, str]:
    """The time zone to use and its name: the setting, else $TZ, else UTC."""
    for n in (name, (os.environ.get("TZ") or "").lstrip(":")):
        if n:
            try:
                return ZoneInfo(n), n
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("unknown time zone %r", n)
    return timezone.utc, "UTC"


def timer_problem(t: Timer, s: ScheduleSettings) -> str | None:
    """Why a timer can't fire, or None."""
    if t.trigger != "time" and (s.latitude is None or s.longitude is None):
        return "Sunrise and sunset need the location (latitude and longitude)."
    if not t.days:
        return "No days selected."
    if t.action == "on" and t.effect and t.effect not in LIBRARY:
        return f"Unknown effect {t.effect!r}."
    return None


def describe(t: Timer) -> str:
    """E.g. "On at sunset +30 min, Mon-Fri"."""
    if t.trigger == "time":
        when = f"at {t.time}"
    else:
        off = "" if not t.offset_min else f" {'+' if t.offset_min > 0 else '−'}{abs(t.offset_min)} min"
        when = f"at {t.trigger}{off}"
    days = "every day" if len(t.days) == 7 else "weekdays" if t.days == [0, 1, 2, 3, 4] else \
        "weekends" if t.days == [5, 6] else ", ".join(DAYS[d] for d in t.days)
    return f"{t.action.capitalize()} {when}, {days}"


def event_time(t: Timer, day: date, s: ScheduleSettings, tz: tzinfo) -> datetime | None:
    """When timer `t` fires for local date `day`, in UTC; None if it doesn't that day."""
    if t.trigger == "time":
        hh, mm = (int(x) for x in t.time.split(":"))
        local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        # A wall time skipped by a DST change round-trips to the hour after; keep that.
        return local.astimezone(timezone.utc)
    if s.latitude is None or s.longitude is None:
        return None
    base = sun_event(day, s.latitude, s.longitude, t.trigger, tz)
    if base is None:
        return None
    # The offset is real minutes, so add it in UTC (wall-clock arithmetic would be off by
    # an hour across a DST change).
    return base.astimezone(timezone.utc) + timedelta(minutes=t.offset_min)


@dataclass
class Event:
    at: datetime  # UTC
    order: int  # the timer's position, to order events at the same instant
    timer: Timer


def events_between(s: ScheduleSettings, start: datetime, end: datetime, tz: tzinfo) -> list[Event]:
    """Events of enabled timers with start < at <= end (both aware), in order."""
    out: list[Event] = []
    if end <= start:
        return out
    # Sun offsets reach ±12 h, so look a day beyond each end.
    first = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    for k, t in enumerate(s.timers):
        if not t.enabled or timer_problem(t, s):
            continue
        day = first
        while day <= last:
            if day.weekday() in t.days:
                at = event_time(t, day, s, tz)
                if at is not None and start < at <= end:
                    out.append(Event(at, k, t))
            day += timedelta(days=1)
    out.sort(key=lambda e: (e.at, e.order))
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, config: ConfigStore, lights: Lights):
        self.config = config
        self.lights = lights
        self.last_check: datetime | None = None
        self.last_fired: dict | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._listeners: list = []

    # ---- queries -------------------------------------------------------------------------
    def tz(self) -> tuple[tzinfo, str]:
        return resolve_tz(self.config.settings.schedule.timezone)

    def upcoming(self, limit: int = 10, days: int = 8, include_disabled: bool = False) -> list[dict]:
        s = self.config.settings.schedule
        if not s.enabled and not include_disabled:
            return []
        tz, _ = self.tz()
        now = _now()
        return [self._event_dict(e, tz) for e in events_between(s, now, now + timedelta(days=days), tz)[:limit]]

    def next_event(self) -> dict | None:
        ev = self.upcoming(limit=1)
        return ev[0] if ev else None

    @staticmethod
    def _event_dict(e: Event, tz: tzinfo) -> dict:
        return {"at": e.at.astimezone(tz).isoformat(), "action": e.timer.action, "timer": e.timer.id,
                "description": describe(e.timer)}

    def status(self) -> dict:
        s = self.config.settings.schedule
        tz, tz_name = self.tz()
        now = _now()
        today = now.astimezone(tz).date()
        sun = None
        if s.latitude is not None and s.longitude is not None:
            sun = {}
            for k, d in (("today", today), ("tomorrow", today + timedelta(days=1))):
                rise = sun_event(d, s.latitude, s.longitude, "sunrise", tz)
                sset = sun_event(d, s.latitude, s.longitude, "sunset", tz)
                sun[k] = {"date": d.isoformat(), "sunrise": rise.isoformat() if rise else None,
                          "sunset": sset.isoformat() if sset else None}
        return {
            "settings": s.model_dump(),
            "timezone": tz_name,
            "timezone_source": "setting" if s.timezone else ("TZ" if tz_name != "UTC" else "default"),
            "now": now.astimezone(tz).isoformat(),
            "sun": sun,
            "upcoming": self.upcoming(include_disabled=True),
            "problems": {t.id: p for t in s.timers if (p := timer_problem(t, s))},
            "descriptions": {t.id: describe(t) for t in s.timers},
            "last_fired": self.last_fired,
        }

    # ---- running -------------------------------------------------------------------------
    def subscribe(self, fn) -> None:  # noqa: ANN001
        """`fn()` after each event fired or schedule change."""
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("schedule listener failed")

    def poke(self) -> None:
        """The schedule changed: re-plan now (callable from any thread)."""
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)
        self._notify()

    def start(self) -> None:
        if not self._task:
            self._loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def check(self, now: datetime | None = None) -> Event | None:
        """Apply the events since the last check; returns the one applied."""
        now = now or _now()
        if self.last_check is None:
            since = now - CATCH_UP
            if self.lights.changed:
                since = max(since, datetime.fromtimestamp(self.lights.changed, timezone.utc))
        elif now < self.last_check:  # the clock went back: don't fire anything twice
            self.last_check = now
            return None
        else:
            since = self.last_check
        self.last_check = now
        s = self.config.settings.schedule
        if not s.enabled:
            return None
        tz, _ = self.tz()
        evs = events_between(s, since, now, tz)
        if not evs:
            return None
        ev = evs[-1]  # the last one decides the state
        await self._apply(ev, tz, skipped=len(evs) - 1)
        return ev

    async def _apply(self, ev: Event, tz: tzinfo, skipped: int = 0) -> None:
        t = ev.timer
        source = f"timer: {describe(t)}"
        try:
            if t.action == "on":
                await self.lights.turn_on(effect=t.effect, brightness=t.brightness, source=source)
            else:
                await self.lights.turn_off(source=source)
            result = "done"
        except LightsBusy as e:
            result = f"skipped: {e}"
        except Exception as e:  # noqa: BLE001
            result = f"failed: {getattr(e, 'detail', None) or e}"
        log.info("timer %s (%s) at %s: %s%s", t.id, describe(t), ev.at.astimezone(tz).isoformat(), result,
                 f" ({skipped} earlier event(s) superseded)" if skipped else "")
        self.last_fired = {**self._event_dict(ev, tz), "result": result}
        self._notify()

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await self.check()
                s = self.config.settings.schedule
                wait = CHECK_S
                if s.enabled:
                    tz, _ = self.tz()
                    now = _now()
                    nxt = events_between(s, now, now + timedelta(seconds=CHECK_S), tz)
                    if nxt:
                        wait = max(0.05, (nxt[0].at - now).total_seconds() + 0.05)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("schedule check failed")
                wait = CHECK_S
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
