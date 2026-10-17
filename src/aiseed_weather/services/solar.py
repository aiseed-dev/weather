# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Solar elevation from the NOAA general solar position approximation.

Open-Meteo's ``is_day`` would need an extra hourly variable in the
forecast, archive and ensemble requests (and their cache keys), so day
versus night is computed locally. The approximation is good to a few
minutes, which is plenty for choosing a sun or moon icon.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# Sun's upper limb on the horizon, with standard refraction: the
# conventional definition of sunrise / sunset.
_SUNRISE_ELEVATION_DEG = -0.833


def solar_elevation_deg(when_utc: datetime, lat: float, lon: float) -> float:
    """Geometric elevation of the sun's centre, in degrees."""
    if when_utc.tzinfo is None:
        raise ValueError("when_utc must be timezone-aware")
    t = when_utc.astimezone(timezone.utc)

    day_of_year = t.timetuple().tm_yday
    hour_frac = t.hour + t.minute / 60 + t.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour_frac - 12) / 24)

    equation_of_time_min = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    true_solar_min = hour_frac * 60 + equation_of_time_min + 4 * lon
    hour_angle = math.radians(true_solar_min / 4 - 180)
    lat_rad = math.radians(lat)
    sin_elevation = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elevation))))


def is_daylight(when_utc: datetime, lat: float, lon: float) -> bool:
    return solar_elevation_deg(when_utc, lat, lon) > _SUNRISE_ELEVATION_DEG
