---
name: open-meteo-access
description: How to fetch point forecasts from Open-Meteo. This is a supporting feature, not the core. Enabled only if the user opted in at first run. Read when modifying the point forecast view or service.
---

## Position in this project

Open-Meteo is the **point forecast** supporting feature. The user enters a
latitude/longitude (or selects from a saved list), and the view shows a full
set of meteorological variables for that point over the next 7 days.

This is **not** the project's core. The core is the ECMWF grid workflow:
maps, animations, climatology overlays. Open-Meteo exists because:

1. A single point full-variable view is useful for users checking specific
   locations (their fields, the Toyama indigo farm, a planned trip)
2. Open-Meteo's `/v1/ecmwf` endpoint serves the same ECMWF data as ECMWF
   Open Data, but pre-processed for point lookups — much cheaper than
   downloading a full grid for one location
3. Open-Meteo provides marine, soil, and air quality variables that are not
   in standard ECMWF Open Data

If the user did not opt into Open-Meteo at first run, the point forecast
view is disabled.

## Implementation: httpx, not the official client

This project uses **httpx directly**, not the `openmeteo-requests` library.
Reasons:

- `openmeteo-requests` adds FlatBuffer decoding and requires `requests-cache`,
  `retry-requests` — three additional dependencies
- Open-Meteo accepts a `format=json` parameter and returns plain JSON
- For occasional user-triggered fetches, the speed advantage of FlatBuffer
  is irrelevant
- Fewer dependencies means a smaller environment file and one less thing
  that could break

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `https://api.open-meteo.com/v1/forecast` | Default "best model" ensemble |
| `https://api.open-meteo.com/v1/ecmwf` | ECMWF-only point data |
| `https://archive-api.open-meteo.com/v1/archive` | Historical reanalysis at point |
| `https://marine-api.open-meteo.com/v1/marine` | Ocean wave and current forecasts |
| `https://air-quality-api.open-meteo.com/v1/air-quality` | Air quality forecast |

This project uses `/v1/forecast` and `/v1/ecmwf` as primary; others can be
added later as supporting features.

## Standard request

```python
import httpx

params = {
    "latitude": 35.0,
    "longitude": 134.0,
    "hourly": ",".join([
        "temperature_2m",
        "relative_humidity_2m",
        "dew_point_2m",
        "apparent_temperature",
        "precipitation",
        "rain",
        "snowfall",
        "pressure_msl",
        "cloud_cover",
        "wind_speed_10m",
        "wind_direction_10m",
        "wind_gusts_10m",
        "shortwave_radiation",
        "soil_temperature_0cm",
        "soil_moisture_0_to_1cm",
    ]),
    "timezone": "auto",
    "forecast_days": 7,
}

async with httpx.AsyncClient(timeout=30.0) as client:
    response = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params=params,
    )
    response.raise_for_status()
    data = response.json()
```

## Response structure

```json
{
  "latitude": 35.0,
  "longitude": 134.0,
  "timezone": "Asia/Tokyo",
  "timezone_abbreviation": "JST",
  "elevation": 412.0,
  "hourly": {
    "time": ["2026-05-14T00:00", "2026-05-14T01:00", ...],
    "temperature_2m": [12.3, 12.1, ...],
    "relative_humidity_2m": [78, 80, ...],
    ...
  },
  "hourly_units": {
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    ...
  }
}
```

Parse `time` as ISO 8601 strings into `numpy.datetime64`. Parse value arrays
as `numpy.ndarray(dtype=float)`. The shape of `hourly[var]` matches the
shape of `hourly["time"]`.

## Decoding pattern

```python
@dataclass(frozen=True)
class PointForecast:
    latitude: float
    longitude: float
    timezone: str
    hourly_times: np.ndarray  # datetime64[s]
    hourly: dict[str, np.ndarray]


def decode(data: dict, variables: tuple[str, ...]) -> PointForecast:
    hourly = data["hourly"]
    times = np.array(hourly["time"], dtype="datetime64[s]")
    decoded = {name: np.asarray(hourly[name], dtype=float) for name in variables}
    return PointForecast(
        latitude=float(data["latitude"]),
        longitude=float(data["longitude"]),
        timezone=data.get("timezone", "UTC"),
        hourly_times=times,
        hourly=decoded,
    )
```

## Caching

- Open-Meteo updates forecasts hourly upstream
- This app caches responses for **1 hour** in `~/.cache/aiseed-weather/openmeteo/`
- Cache filename encodes the request: `{lat}_{lon}_{model}_{days}d_{var_hash}.json`
- Different requests (different lat/lon, different variables) never share cache entries
- Cache is JSON for inspectability (the response is JSON anyway)

```python
def cache_path(lat, lon, variables, days, model):
    var_hash = abs(hash(variables)) % (10 ** 8)
    return cache_dir / f"{lat:.4f}_{lon:.4f}_{model}_{days}d_{var_hash}.json"


def is_fresh(path: Path, max_age_seconds: int = 3600) -> bool:
    if not path.exists():
        return False
    return time.time() - path.stat().st_mtime < max_age_seconds
```

## User-action fetch (see `user-action-fetch` skill)

Standard pattern:
- Mount the view → check cache → fetch if stale
- User presses Refresh → force=True → bypass cache
- User changes coordinates → new request → check cache for new coords

## Attribution (mandatory)

Open-Meteo is free for non-commercial use and asks for attribution. Every
point forecast figure or display must show:

> Data: Open-Meteo (https://open-meteo.com). CC-BY-4.0

For the ECMWF-only endpoint, also include:

> Source: ECMWF Open Data via Open-Meteo. CC-BY-4.0

This goes in the figure footer via `figures/footer.py`.

## Rate limits

Open-Meteo's free tier allows 10,000 requests per day per IP for non-commercial
use. This is far more than this app will ever generate (one request per view
mount, cached for an hour) — no rate limiting logic needed beyond the cache.

If the user makes many requests in quick succession (e.g. comparing multiple
points), the cache catches all repeats. Distinct points within an hour still
fit easily under the limit.

## Forbidden

- Using the `openmeteo-requests` client (use httpx directly)
- Adding `requests-cache` or `retry-requests` dependencies (not needed)
- Using Open-Meteo for the main map view (use ECMWF Open Data)
- Background polling (see `user-action-fetch` skill)
- Caching beyond 1 hour (the data updates hourly)
- Calling Open-Meteo if the user did not opt in at first run
- Hardcoding the variable list outside `services/point_forecast_service.py`
- Forgetting attribution on exported figures
- Importing `flet` in this directory
