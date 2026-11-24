"""Sunrise and sunset times, from NOAA's solar calculator (no network, no dependency).

The equations are those of NOAA's General Solar Position spreadsheet (after Meeus,
*Astronomical Algorithms*), the same ones the `astral` package uses. They are accurate
to about a minute between the polar circles. Sunrise and sunset are when the top of
the sun's disc is on the horizon, with standard refraction: zenith 90.833°.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, tzinfo

ZENITH_OFFICIAL = 90.833
_JD_ORDINAL = 1721424.5  # Julian day at 0h UT of date.toordinal() == 0


def _solar(jd: float) -> tuple[float, float]:
    """Declination (radians) and the equation of time (minutes) at Julian day `jd`."""
    t = (jd - 2451545.0) / 36525.0  # Julian centuries since J2000.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0  # geometric mean longitude
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)  # mean anomaly
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)  # orbit eccentricity
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t) + math.sin(3 * mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(app_long))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eqt = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                           - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    return decl, eqt


def _utc_event(d: date, lat: float, lon: float, rising: bool, zenith: float) -> datetime | None:
    """The event nearest to solar noon of UTC date `d`, in UTC; None if the sun doesn't
    cross that zenith (polar day or night)."""
    lat_r = math.radians(max(-89.8, min(89.8, lat)))
    jd0 = d.toordinal() + _JD_ORDINAL
    minutes = 720.0 - 4.0 * lon  # first guess: solar noon
    for _ in range(3):  # declination and equation of time at the event itself
        decl, eqt = _solar(jd0 + minutes / 1440.0)
        cos_h = (math.cos(math.radians(zenith)) - math.sin(lat_r) * math.sin(decl)) / (math.cos(lat_r) * math.cos(decl))
        if not -1.0 <= cos_h <= 1.0:
            return None
        h = math.degrees(math.acos(cos_h))
        minutes = 720.0 - 4.0 * (lon + (h if rising else -h)) - eqt
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def sun_event(day: date, lat: float, lon: float, event: str, tz: tzinfo, zenith: float = ZENITH_OFFICIAL) -> datetime | None:
    """Sunrise or sunset (`event`) on local date `day` in time zone `tz`, as an aware datetime
    in `tz`; None where the sun doesn't rise or set that day. Longitude is positive east."""
    if event not in ("sunrise", "sunset"):
        raise ValueError(f"unknown sun event {event!r}")
    rising = event == "sunrise"
    # The UTC date of the local day's event can be the day before or after (far from Greenwich).
    for k in (0, -1, 1):
        t = _utc_event(day + timedelta(days=k), lat, lon, rising, zenith)
        if t is not None and t.astimezone(tz).date() == day:
            return t.astimezone(tz)
    return None
