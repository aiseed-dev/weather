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
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class Location:
    """One user-saved point. Frozen so it's safe to use as a dict key
    and to pass through the @ft.observable session machinery."""

    name: str
    latitude: float
    longitude: float
    is_japan: bool
    created_at: datetime

    @classmethod
    def new(cls, name: str, latitude: float, longitude: float) -> "Location":
        """Construct a Location, deriving is_japan and a UTC created_at."""
        return cls(
            name=name.strip(),
            latitude=float(latitude),
            longitude=float(longitude),
            is_japan=is_in_japan(latitude, longitude),
            created_at=datetime.now(timezone.utc),
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
                created = datetime.now(timezone.utc)
        elif created is None:
            created = datetime.now(timezone.utc)
        return cls(
            name=str(data["name"]),
            latitude=float(data["latitude"]),
            longitude=float(data["longitude"]),
            is_japan=bool(data.get(
                "is_japan",
                is_in_japan(float(data["latitude"]), float(data["longitude"])),
            )),
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
