"""Sunrise/sunset, timer times (with daylight saving) and the scheduler's firing rules."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from treelights.config import ConfigStore, ScheduleSettings, Timer
from treelights.lights import LightsBusy
from treelights.schedule import Scheduler, describe, event_time, events_between, resolve_tz, timer_problem
from treelights.sun import sun_event

LONDON = ZoneInfo("Europe/London")
BERLIN = ZoneInfo("Europe/Berlin")


def _close(t: datetime, hh: int, mm: int, tol_min: float = 2) -> bool:
    ref = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return abs((t - ref).total_seconds()) <= tol_min * 60


def test_sun_times_match_published_tables():
    # London (51.5072 N, 0.1276 W). Published (timeanddate.com): 21 Dec sunrise 08:04, sunset 15:53;
    # 21 Jun sunrise 04:43, sunset 21:21 (BST).
    assert _close(sun_event(date(2026, 12, 21), 51.5072, -0.1276, "sunrise", LONDON), 8, 4)
    assert _close(sun_event(date(2026, 12, 21), 51.5072, -0.1276, "sunset", LONDON), 15, 53)
    june = sun_event(date(2026, 6, 21), 51.5072, -0.1276, "sunrise", LONDON)
    assert _close(june, 4, 43) and june.utcoffset() == timedelta(hours=1)
    assert _close(sun_event(date(2026, 6, 21), 51.5072, -0.1276, "sunset", LONDON), 21, 21)


def test_sun_event_is_on_the_local_date_far_from_greenwich():
    # Auckland (UTC+13 in December) and Honolulu (UTC-10): the UTC date differs from the local one.
    for lat, lon, tz in ((-36.85, 174.76, "Pacific/Auckland"), (21.31, -157.86, "Pacific/Honolulu")):
        z = ZoneInfo(tz)
        for d in (date(2026, 12, 1), date(2026, 6, 1)):
            for ev in ("sunrise", "sunset"):
                t = sun_event(d, lat, lon, ev, z)
                assert t is not None and t.date() == d
        assert sun_event(d, lat, lon, "sunrise", z) < sun_event(d, lat, lon, "sunset", z)


def test_polar_night_and_midnight_sun_have_no_event():
    oslo = ZoneInfo("Europe/Oslo")  # Tromsø
    assert sun_event(date(2026, 12, 21), 69.65, 18.96, "sunrise", oslo) is None
    assert sun_event(date(2026, 6, 21), 69.65, 18.96, "sunset", oslo) is None


def test_fixed_time_follows_daylight_saving():
    t = Timer(action="on", trigger="time", time="18:00")
    s = ScheduleSettings(timezone="Europe/Berlin", timers=[t])
    winter = event_time(t, date(2026, 3, 28), s, BERLIN)  # the day before clocks go forward
    summer = event_time(t, date(2026, 3, 29), s, BERLIN)
    assert winter == datetime(2026, 3, 28, 17, 0, tzinfo=timezone.utc)
    assert summer == datetime(2026, 3, 29, 16, 0, tzinfo=timezone.utc)
    # 02:30 doesn't exist on 29 March in Berlin: it fires at 03:30 local (01:30 UTC).
    skipped = Timer(action="off", trigger="time", time="02:30")
    assert event_time(skipped, date(2026, 3, 29), s, BERLIN) == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    # 02:30 occurs twice on 25 October: it fires once.
    s2 = ScheduleSettings(timezone="Europe/Berlin", timers=[skipped])
    evs = events_between(s2, datetime(2026, 10, 24, 12, tzinfo=timezone.utc), datetime(2026, 10, 25, 12, tzinfo=timezone.utc), BERLIN)
    assert len(evs) == 1


def test_sun_offset_is_in_real_minutes_and_days_filter():
    s = ScheduleSettings(timezone="Europe/London", latitude=51.5072, longitude=-0.1276)
    t = Timer(action="on", trigger="sunset", offset_min=-30, days=[0, 1, 2, 3, 4])
    base = sun_event(date(2026, 12, 21), 51.5072, -0.1276, "sunset", LONDON)  # a Monday
    assert event_time(t, date(2026, 12, 21), s, LONDON) == base.astimezone(timezone.utc) - timedelta(minutes=30)
    s.timers = [t]
    start = datetime(2026, 12, 21, 0, 0, tzinfo=timezone.utc)
    evs = events_between(s, start, start + timedelta(days=7), LONDON)
    assert [e.at.astimezone(LONDON).weekday() for e in evs] == [0, 1, 2, 3, 4]
    assert describe(t) == "On at sunset −30 min, weekdays"


def test_problems_disabled_timers_and_time_zone_fallback(monkeypatch):
    s = ScheduleSettings(timers=[Timer(action="on", trigger="sunrise"), Timer(action="off", time="23:00", enabled=False),
                                 Timer(action="on", time="07:00", effect="nope")])
    assert "location" in timer_problem(s.timers[0], s)
    assert "Unknown effect" in timer_problem(s.timers[2], s)
    start = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert events_between(s, start, start + timedelta(days=3), timezone.utc) == []
    monkeypatch.setenv("TZ", "America/New_York")
    assert resolve_tz(None)[1] == "America/New_York"
    assert resolve_tz("Europe/Berlin")[1] == "Europe/Berlin"
    monkeypatch.delenv("TZ")
    assert resolve_tz(None)[1] == "UTC"
    with pytest.raises(ValueError):
        ScheduleSettings(timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        Timer(time="25:00")


class FakeLights:
    def __init__(self, changed: float = 0.0):
        self.changed = changed
        self.calls: list = []
        self.busy = False

    async def turn_on(self, effect=None, brightness=None, source=""):
        if self.busy:
            raise LightsBusy("scan")
        self.calls.append(("on", effect, brightness))

    async def turn_off(self, source=""):
        if self.busy:
            raise LightsBusy("scan")
        self.calls.append(("off",))


async def test_scheduler_fires_catches_up_and_ignores_clock_going_back(tmp_path):
    cfg = ConfigStore(tmp_path)
    cfg.update({"schedule": {"timezone": "UTC", "timers": [
        {"action": "on", "time": "17:00", "effect": "solid", "brightness": 0.3},
        {"action": "off", "time": "23:00"}]}})
    t0 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    # The lights were last switched at 16:00; the server starts at 18:00: the 17:00 "on" is caught up.
    lights = FakeLights(changed=(t0 + timedelta(hours=4)).timestamp())
    sch = Scheduler(cfg, lights)
    ev = await sch.check(t0 + timedelta(hours=6))
    assert ev and ev.timer.action == "on" and lights.calls == [("on", "solid", 0.3)]
    assert sch.last_fired["result"] == "done"
    # Nothing new until 23:00.
    assert await sch.check(t0 + timedelta(hours=10)) is None
    # The clock goes back: nothing fires twice.
    assert await sch.check(t0 + timedelta(hours=5)) is None
    assert await sch.check(t0 + timedelta(hours=6)) is None and len(lights.calls) == 1
    # Several events in one check (e.g. the host was suspended): only the last one applies.
    await sch.check(t0 + timedelta(days=1, hours=6))  # 23:00 off, then 17:00 on next day -> on
    assert lights.calls[-1][0] == "on" and len(lights.calls) == 2
    # Busy (a scan holds the LEDs): skipped, and reported.
    lights.busy = True
    await sch.check(t0 + timedelta(days=1, hours=11, minutes=1))
    assert sch.last_fired["result"].startswith("skipped")
    # Disabled schedule: nothing fires.
    lights.busy = False
    cfg.update({"schedule": {"enabled": False}})
    assert await sch.check(t0 + timedelta(days=2, hours=12)) is None


async def test_scheduler_catch_up_respects_a_later_manual_change(tmp_path):
    cfg = ConfigStore(tmp_path)
    cfg.update({"schedule": {"timezone": "UTC", "timers": [{"action": "on", "time": "17:00"}]}})
    t0 = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)
    # Switched off by hand at 19:00, after the 17:00 "on": a restart at 20:00 leaves them off.
    lights = FakeLights(changed=(t0 + timedelta(hours=19)).timestamp())
    assert await Scheduler(cfg, lights).check(t0 + timedelta(hours=20)) is None and lights.calls == []
