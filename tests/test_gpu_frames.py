# SPDX-License-Identifier: AGPL-3.0-or-later
"""services/gpu_frames: data for the GPU map view (flet-gpufield)."""
from __future__ import annotations

import numpy as np
import pytest

from aiseed_weather.figures.regions import ARCTIC, EUROPE, GLOBAL, JAPAN
from aiseed_weather.services import gpu_frames as g


def _decode_scalar(data: bytes, domain: g.GpuDomain, vmin: float, vmax: float) -> np.ndarray:
    rgba = np.frombuffer(data, np.uint8).reshape(domain.ny, domain.nx, 4)
    q = (rgba[..., 0].astype(np.float64) * 256 + rgba[..., 1]) / 65535.0
    out = vmin + q * (vmax - vmin)
    out[rgba[..., 3] < 128] = np.nan
    return out


def test_domains():
    d = g.domain_for(JAPAN)
    assert (d.projection, d.lon0, d.lat0, d.stride, d.nx, d.ny, d.periodic) == \
        ("plate_carree", 95.0, 65.0, 1, 361, 281, False)
    d = g.domain_for(GLOBAL)
    assert (d.nx, d.ny, d.stride, d.periodic) == (720, 361, 2, True)
    d = g.domain_for(ARCTIC)
    assert (d.projection, d.nx, d.ny, d.lat0, d.periodic) == ("polar_north", 720, 141, 90.0, True)
    d = g.domain_for(EUROPE)
    assert d.lon0 < 0          # crosses the 0° meridian with negative longitudes


def test_crop_wraps_across_greenwich():
    lon_index = np.tile(np.arange(g.N_LON), (g.N_LAT, 1)).astype(np.float32)
    d = g.domain_for(EUROPE)
    c = d.crop(lon_index)
    assert c.shape == (d.ny, d.nx)
    assert c[0, 0] == round((d.lon0 % 360) / g.RES)
    # longitude increases by one 0.25° column per column, through 360 → 0
    steps = np.diff(c[0]) % g.N_LON
    assert np.all(steps == d.stride)


def test_land_mask_matches_geography():
    d = g.domain_for(JAPAN)
    land = np.frombuffer(g.land_mask(d), np.uint8).reshape(d.ny, d.nx)

    def at(lat, lon):
        return land[int(round((d.lat0 - lat) / d.dlat)), int(round((lon - d.lon0) / d.dlon))]

    assert at(35.7, 139.7) == 1     # Tokyo
    assert at(30.0, 150.0) == 0     # Pacific


def test_coastline_has_no_wrap_jumps():
    for region in (JAPAN, EUROPE):
        pts = np.frombuffer(g.coastline(g.domain_for(region)), "<f4").reshape(-1, 2)
        assert len(pts) > 1000
        seg = np.abs(np.diff(pts[:, 0]))
        assert np.nanmax(seg) < 5.0


def test_pack_scalar_round_trip():
    d = g.domain_for(JAPAN)
    values = np.linspace(-20, 40, d.nx * d.ny, dtype=np.float32).reshape(d.ny, d.nx)
    values[0, 0] = np.nan
    back = _decode_scalar(g._pack_scalar(values, -20.0, 40.0), d, -20.0, 40.0)
    assert np.isnan(back[0, 0])
    assert np.nanmax(np.abs(back - values)) <= 60.0 / 65535 + 1e-6


def test_pack_wind_round_trip():
    u = np.array([[1.234, -5.0], [np.nan, 30.0]], np.float32)
    v = np.array([[0.0, 2.5], [3.0, -40.0]], np.float32)
    raw = np.frombuffer(g._pack_wind(u, v), "<i2").astype(np.float32) / 100.0
    assert np.allclose(raw[:4].reshape(2, 2), np.nan_to_num(u), atol=0.006)
    assert np.allclose(raw[4:].reshape(2, 2), v, atol=0.006)


def test_layer_mapping():
    for key in ("msl", "t2m", "wind10m", "tp", "t850", "gh500", "wind250", "wind500"):
        assert g.layer_for(key) is not None, key
    assert g.layer_for("no_such_layer") is None
    assert g.kinds_for(g.layer_for("msl"), msl_overlay=False) == ("sfc",)
    assert g.kinds_for(g.layer_for("gh500"), msl_overlay=True) == ("pl", "sfc")
    assert g.layer_for("tp").interval_precip


@pytest.fixture
def synthetic_grib(tmp_path):
    """A global 0.25° GRIB2 with msl, 10 m wind and 500 hPa gh/u/v."""
    eccodes = pytest.importorskip("eccodes")
    lat = np.linspace(90, -90, g.N_LAT)[:, None]
    lon = np.arange(g.N_LON)[None, :] * g.RES
    msl = 101300.0 + 2000.0 * np.sin(np.radians(lat)) * np.cos(np.radians(lon))   # Pa
    fields = [
        (151, "meanSea", 0, msl),
        (165, "heightAboveGround", 10, np.full_like(msl, 5.0)),
        (166, "heightAboveGround", 10, np.full_like(msl, -3.0)),
        (156, "isobaricInhPa", 500, 5600.0 + 300.0 * np.cos(np.radians(lat)) + 0 * lon),
        (131, "isobaricInhPa", 500, np.full_like(msl, 20.0)),
        (132, "isobaricInhPa", 500, np.full_like(msl, 0.0)),
    ]
    path = tmp_path / "24h.grib2"
    with open(path, "wb") as f:
        for pid, tol, level, data in fields:
            h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            for key, val in (("Ni", g.N_LON), ("Nj", g.N_LAT),
                             ("latitudeOfFirstGridPointInDegrees", 90.0),
                             ("longitudeOfFirstGridPointInDegrees", 0.0),
                             ("latitudeOfLastGridPointInDegrees", -90.0),
                             ("longitudeOfLastGridPointInDegrees", 359.75),
                             ("iDirectionIncrementInDegrees", 0.25),
                             ("jDirectionIncrementInDegrees", 0.25),
                             ("typeOfLevel", tol), ("level", level), ("paramId", pid)):
                eccodes.codes_set(h, key, val)
            eccodes.codes_set(h, "bitsPerValue", 24)
            eccodes.codes_set_values(h, np.broadcast_to(data, (g.N_LAT, g.N_LON)).ravel())
            eccodes.codes_write(h, f)
            eccodes.codes_release(h)
    return path, msl


def test_frame_from_grib(synthetic_grib):
    path, msl = synthetic_grib
    d = g.domain_for(JAPAN)
    layer = g.layer_for("msl")
    _, vmin, vmax = g.lut_rgba(layer)
    frame = g.FrameBuilder(layer, d).build({"sfc": path, "pl": path})
    got = _decode_scalar(frame.scalar, d, vmin, vmax)
    want = d.crop(msl.astype(np.float32)) / 100.0          # the spec converts Pa → hPa
    inside = (want > vmin) & (want < vmax)
    assert np.nanmax(np.abs(got - want)[inside]) < 0.01
    assert frame.contours and frame.contours_bold and frame.labels
    wind = np.frombuffer(frame.wind, "<i2").reshape(2, d.ny, d.nx) / 100.0
    assert np.allclose(wind[0], 5.0) and np.allclose(wind[1], -3.0)


def test_pressure_level_frame_and_msl_overlay(synthetic_grib):
    path, _ = synthetic_grib
    d = g.domain_for(GLOBAL)
    frame = g.FrameBuilder(g.layer_for("gh500"), d, msl_overlay=True).build({"sfc": path, "pl": path})
    wind = np.frombuffer(frame.wind, "<i2").reshape(2, d.ny, d.nx) / 100.0
    assert np.allclose(wind[0], 20.0)          # particles use the 500 hPa wind
    # gh500 has its own isohypses, so the MSL overlay is not used
    lines = np.frombuffer(frame.contours or frame.contours_bold, "<f4").reshape(-1, 2)
    assert np.nanmin(lines[:, 1]) >= -90 and np.nanmax(lines[:, 1]) <= 90
