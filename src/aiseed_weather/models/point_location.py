# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""User-saved locations for the 地点 (point forecast) view.

A Location is a name plus a latitude / longitude pair that drives all
Open-Meteo API calls for the point-forecast tab. Locations are
persisted as a small JSON file under the user-settings data dir;
JSON is the right format here because the file is short, hand-
editable, and the value count rarely exceeds the dozens.

See docs/forecast-spec.md.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
# Alias to avoid name clash with the ``timezone: str`` Location field
# below — we still need stdlib ``timezone.utc`` for UTC stamps.
from datetime import datetime, timezone as _tz
from pathlib import Path

logger = logging.getLogger(__name__)


# Bounding box used to flag a location as 日本国内 — drives whether the
# Open-Meteo JMA MSM 5 km reference forecast is fetched alongside the
# main ECMWF IFS HRES forecast. Generous box covers main islands +
# Okinawa + the surrounding territorial waters.
JAPAN_LAT_MIN, JAPAN_LAT_MAX = 24.0, 46.0
JAPAN_LON_MIN, JAPAN_LON_MAX = 122.0, 146.0


def is_in_japan(latitude: float, longitude: float) -> bool:
    """Whether (lat, lon) falls inside the JMA MSM coverage box."""
    return (
        JAPAN_LAT_MIN <= latitude <= JAPAN_LAT_MAX
        and JAPAN_LON_MIN <= longitude <= JAPAN_LON_MAX
    )


def default_timezone_for(latitude: float, longitude: float) -> str:
    """Default IANA timezone name for a (lat, lon).

    Stays simple: JP bounding box → 'Asia/Tokyo', otherwise 'UTC'.
    The user can override at location-add time via the dialog. A
    real lat/lon → tz resolver (e.g. timezonefinder) would be more
    accurate but adds a heavy dependency the project doesn't need
    when the user is happy to type a timezone themselves.
    """
    return "Asia/Tokyo" if is_in_japan(latitude, longitude) else "UTC"


@dataclass(frozen=True)
class Location:
    """One user-saved point. Frozen so it's safe to use as a dict key
    and to pass through the @ft.observable session machinery."""

    name: str
    latitude: float
    longitude: float
    is_japan: bool
    timezone: str           # IANA name; drives the chart's clock
    created_at: datetime

    @classmethod
    def new(
        cls, name: str, latitude: float, longitude: float,
        timezone_name: str | None = None,
    ) -> "Location":
        """Construct a Location, deriving is_japan and a UTC
        ``created_at``. ``timezone_name`` overrides the
        lat/lon-derived default — passed when the user explicitly
        picks a tz in the add-location dialog."""
        tz = (
            timezone_name.strip()
            if timezone_name and timezone_name.strip()
            else default_timezone_for(latitude, longitude)
        )
        return cls(
            name=name.strip(),
            latitude=float(latitude),
            longitude=float(longitude),
            is_japan=is_in_japan(latitude, longitude),
            timezone=tz,
            created_at=datetime.now(_tz.utc),
        )

    def to_json(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d

    @classmethod
    def from_json(cls, data: dict) -> "Location":
        created = data.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                created = datetime.now(_tz.utc)
        elif created is None:
            created = datetime.now(_tz.utc)
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        # Backward compat: locations.json from before the timezone
        # field existed defaults to the lat/lon-derived guess.
        tz = str(data.get("timezone") or default_timezone_for(lat, lon))
        return cls(
            name=str(data["name"]),
            latitude=lat,
            longitude=lon,
            is_japan=bool(data.get("is_japan", is_in_japan(lat, lon))),
            timezone=tz,
            created_at=created,
        )


# Replace runs of filesystem-unfriendly characters with a single '_' so
# the Parquet archive directory and the user-visible name can use the
# same string. Multibyte names (Japanese place names, etc.) are kept
# verbatim — modern filesystems accept them.
_FS_SAFE_RE = re.compile(r"[\\/:\*\?\"<>\|\x00-\x1f]+")


def location_safe_dirname(name: str) -> str:
    safe = _FS_SAFE_RE.sub("_", name).strip().strip(".")
    return safe or "location"


def locations_file(data_dir: Path) -> Path:
    """Where the locations JSON lives. Inside the user's data dir to
    keep config + accumulated data together."""
    return data_dir / "point_forecast" / "locations.json"


def load_locations(data_dir: Path) -> list[Location]:
    path = locations_file(data_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("locations.json unreadable; treating as empty")
        return []
    if not isinstance(raw, list):
        return []
    out: list[Location] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Location.from_json(item))
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "Skipping malformed location entry: %r", item,
            )
    return out


def save_locations(data_dir: Path, locations: list[Location]) -> None:
    """Write the list atomically (temp + rename) so a crash mid-write
    can never leave a half-truncated locations.json."""
    path = locations_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            [loc.to_json() for loc in locations],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)
