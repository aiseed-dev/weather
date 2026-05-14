---
name: climatology-analysis
description: How to compute climatologies, anomalies, and percentile rankings from ERA5. Read when implementing any feature that compares current conditions to historical context. This is the project's core differentiator.
---

## Why this skill exists

Many tools show today's weather. This project's distinguishing feature is
**putting today in 85 years of context**. Every climatology/anomaly feature
goes through the patterns in this document so the math is consistent and
the figure labels are honest.

## Definitions (use these terms exactly in code and UI)

- **Climatology**: a long-term average for a given calendar position
  (e.g. "average MSL on May 14, 1991-2020").
- **Reference period** (or "baseline" or "normal"): the year range used to
  compute the climatology. Default: **1991-2020** (current WMO normal).
- **Anomaly**: current value minus climatology, same units.
- **Standardized anomaly** (or "z-score"): anomaly divided by the climatology's
  standard deviation. Unitless. Use when comparing across variables or regions.
- **Percentile rank**: where today's value falls in the historical distribution
  for this calendar day (e.g. "warmer than 97% of May 14ths since 1940").

Never use "above normal" without specifying the reference period in the same
figure.

## Reference period rules

- Default: **1991-2020** (WMO standard normal, valid until 2030)
- Make user-configurable, but always display the chosen period on the figure
- For events older than the reference period: this is OK and informative
  ("the 1972 event vs the 1991-2020 normal"), but flag it in metadata
- For sub-periods (e.g. 2010-2020 "recent normal"): allow but require explicit
  user opt-in; the WMO default is the safe choice

## Standard computations

### Daily climatology (per calendar day)

```python
# Per day-of-year mean across the reference period.
clim = (
    era5_ds
    .sel(time=slice("1991-01-01", "2020-12-31"))
    .groupby("time.dayofyear")
    .mean("time")
)
```

### Daily climatology with smoothing

Per-day means are noisy. Smooth with a centered 31-day rolling mean before
publication-quality work:

```python
clim_smooth = clim.rolling(dayofyear=31, center=True).mean()
```

The 31-day window is the conventional choice; document the smoothing on the
figure ("31-day smoothed daily climatology").

### Anomaly

```python
doy = current_field["time"].dt.dayofyear
anomaly = current_field - clim.sel(dayofyear=doy)
```

### Standardized anomaly

```python
clim_std = (
    era5_ds
    .sel(time=slice("1991-01-01", "2020-12-31"))
    .groupby("time.dayofyear")
    .std("time")
)
z = anomaly / clim_std.sel(dayofyear=doy)
```

### Percentile rank

```python
ref_for_day = era5_ds.sel(time=era5_ds["time"].dt.dayofyear == doy)
rank = (ref_for_day <= current_field).sum("time") / ref_for_day.sizes["time"]
# rank=0.97 means "warmer than 97% of historical instances on this day"
```

## Visualization conventions

### Anomaly color scales

- **Use diverging colormaps**, centered at zero. Never sequential.
  - Temperature anomaly: `RdBu_r`
  - Pressure anomaly: `BrBG_r` (or `RdBu_r` if user prefers)
  - Precipitation anomaly: `BrBG` (brown = dry, green = wet)
- **Always symmetric range** (e.g. `vmin=-10, vmax=+10`). Asymmetric ranges
  mislead readers about where "normal" is.
- **Lock the range across timesteps** in animations, so frames are comparable.

### Standardized anomaly scales

- Symmetric, typically ±3 sigma
- Mark ±2 sigma with contour lines (the "unusual" threshold)
- Use a single colormap project-wide for z-scores so users learn it

### Labels (mandatory)

Every anomaly figure must show:

1. The variable and unit
2. The reference period (e.g. "vs 1991-2020")
3. Whether climatology is smoothed (and how)
4. The data run identifier
5. CC-BY-4.0 attribution

## Caching

Climatologies are expensive to compute and immutable for a given
(variable, reference period). Cache aggressively:

- Path: `~/.cache/aiseed-weather/climatology/`
- Key: `{var}_{ref_start}_{ref_end}_{smoothing}.nc`
- Computed once per machine; never recompute unless cache is missing or the
  user explicitly forces a rebuild

Compute lazily on first request, save with `xarray.Dataset.to_netcdf`.

## Performance

- Daily climatology over 30 years is ~11,000 timesteps. With xarray + Dask
  this is manageable but not instant.
- Show a progress indicator the first time per (variable, period); subsequent
  uses are cache hits.
- Pre-compute the most common cases on first run (e.g. msl, 2t, gh500 against
  1991-2020) and announce it: "Building climatology cache — about 2 minutes,
  one time."

## Forbidden

- Computing an anomaly without recording the reference period
- Asymmetric color ranges around zero
- "Normal" without a period qualifier in any user-facing text
- Recomputing climatology on every request (always cache)
- Mixing reference periods between adjacent figures without labeling
