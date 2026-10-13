# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
"""JMA nowcast: tile decoding, the time index, the mosaic and its resampling."""

import asyncio
import io
import time

import httpx
import numpy as np
import pytest
from PIL import Image

from aiseed_weather.figures._japan_map import EXTENT
from aiseed_weather.figures.radar_map import render_radar_map, resample_to_frame
from aiseed_weather.services import jma_radar_service as jr

JMA_RGB = [(242, 242, 255), (160, 210, 255), (33, 140, 255), (0, 65, 255),
           (250, 245, 0), (255, 153, 0), (255, 40, 0), (180, 0, 104)]


def _palette_tile(classes: np.ndarray) -> bytes:
    """A tile the way JMA serves it: palette PNG, index 0 transparent."""
    im = Image.fromarray(classes.astype(np.uint8), mode="P")
    im.putpalette([255, 255, 255] + [v for c in JMA_RGB for v in c])
    buf = io.BytesIO()
    im.save(buf, format="PNG", transparency=0)
    return buf.getvalue()


def test_decode_tile_maps_legend_colours_to_classes():
    classes = np.zeros((256, 256), np.uint8)
    for k in range(1, 9):
        classes[k * 10:k * 10 + 5, :] = k
    assert np.array_equal(jr.decode_tile(_palette_tile(classes)), classes)


def test_decode_tile_maps_an_unknown_colour_to_the_nearest(caplog):
    rgba = np.zeros((256, 256, 4), np.uint8)
    rgba[0, 0] = (250, 150, 10, 255)              # close to 30〜50 mm/h orange
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    out = jr.decode_tile(buf.getvalue())
    assert out[0, 0] == 6 and out.sum() == 6
    rgba[0, 0] = (90, 200, 90, 255)               # nothing like the legend
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    jr.decode_tile(buf.getvalue())
    assert "not in the legend" in caplog.text


def test_tile_range_and_pixel_geometry():
    assert jr.tile_range(EXTENT, 6) == (53, 58, 22, 27)
    m = jr.RadarMosaic(frame=jr.RadarFrame("20261013072000", "20261013072000"), zoom=6,
                       x0=53, y0=22, classes=np.zeros((6 * 256, 6 * 256), np.uint8),
                       tiles=36, missing=0, fetched_at=None)
    lons, lats = m.lons(), m.lats()
    assert lons[0] == pytest.approx(53 / 64 * 360 - 180 + 360 / (64 * 256) / 2)
    assert np.all(np.diff(lons) > 0) and np.all(np.diff(lats) < 0)
    assert lons[0] < EXTENT[0] and lons[-1] > EXTENT[1]
    assert lats[0] > EXTENT[3] and lats[-1] < EXTENT[2]


def _index():
    obs = [{"basetime": t, "validtime": t, "elements": ["hrpns", "hrpns_nd"]}
           for t in ("20261013072000", "20261013071500", "20261013071000")]
    fcst = [{"basetime": "20261013072000", "validtime": v, "elements": ["hrpns", "hrpns_nd"]}
            for v in ("20261013073000", "20261013072500")]
    return obs, fcst


def test_time_index_orders_observations_then_forecast():
    obs, fcst = _index()
    t = jr.JmaRadarService._times_from({"fetched_at": "2026-10-13T07:24:00+00:00",
                                        "observations": obs, "forecasts": fcst})
    assert [f.validtime[-6:-2] for f in t.frames] == ["0710", "0715", "0720", "0725", "0730"]
    assert t.latest_observation.validtime == "20261013072000"
    f = t.frames[-1]
    assert f.is_forecast and f.lead_minutes == 10
    assert t.find(f.key) is f
    with pytest.raises(ValueError):
        jr.JmaRadarService._times_from({"fetched_at": "2026-10-13T07:24:00+00:00",
                                        "observations": [], "forecasts": fcst})


def _client_factory(monkeypatch, handler):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(jr.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


def test_mosaic_fetches_once_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(jr, "MAX_REQUESTS_PER_SECOND", 1000)
    seen = []
    tile = np.zeros((256, 256), np.uint8)
    tile[100:110, 100:110] = 7

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["user-agent"].startswith("AIseed Weather")
        if request.url.path.endswith("/57/25.png"):
            return httpx.Response(404)
        return httpx.Response(200, content=_palette_tile(tile))

    _client_factory(monkeypatch, handler)
    svc = jr.JmaRadarService(data_dir=tmp_path)
    frame = jr.RadarFrame("20991231235500", "20991231235500")   # never pruned
    extent = (135.0, 141.0, 33.0, 37.0)                          # 2 × 2 tiles at z6
    progress = []
    m = asyncio.run(svc.mosaic(frame, extent, 6, on_progress=lambda d, n: progress.append((d, n))))
    assert (m.x0, m.y0, m.classes.shape, m.tiles, m.missing) == (56, 24, (512, 512), 4, 1)
    assert progress[-1] == (4, 4)
    assert all("/hrpns/6/" in p and "/20991231235500/none/20991231235500/" in p for p in seen)
    assert m.classes[100:110, 100:110].min() == 7          # tile (56, 24), top left
    assert m.classes[100:110, 356:366].min() == 7          # tile (57, 24), top right
    assert m.classes[356:366, 356:366].max() == 0          # tile (57, 25) was a 404
    n = len(seen)
    asyncio.run(svc.mosaic(frame, extent, 6))
    assert len(seen) == n + 1                               # only the missing tile again
    with pytest.raises(ValueError):
        asyncio.run(svc.mosaic(frame, extent, 5))


def test_mosaic_with_no_tile_delivered_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(jr, "MAX_REQUESTS_PER_SECOND", 1000)
    _client_factory(monkeypatch, lambda request: httpx.Response(503))
    svc = jr.JmaRadarService(data_dir=tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(svc.mosaic(jr.RadarFrame("20991231235500", "20991231235500"),
                               (135.0, 141.0, 33.0, 37.0), 6))


def test_request_pacer_spaces_requests():
    async def run():
        pacer = jr._RequestPacer(20)                        # one start per 50 ms
        t0 = time.monotonic()
        for _ in range(5):
            await pacer.wait()
        return time.monotonic() - t0
    assert asyncio.run(run()) >= 0.19


def test_old_basetimes_are_pruned(tmp_path):
    svc = jr.JmaRadarService(data_dir=tmp_path)
    old = tmp_path / "jma" / "radar" / "20000101000000"
    new = tmp_path / "jma" / "radar" / "20991231235500"
    other = tmp_path / "jma" / "radar" / "notes"
    for d in (old, new, other):
        d.mkdir(parents=True)
    svc._prune()
    assert not old.exists() and new.exists() and other.exists()


def test_resample_keeps_the_strongest_pixel_and_fills_upsampling():
    lons = np.arange(100) + 0.5          # 1° pixels, 0 … 100°E
    lats = 50.0 - (np.arange(50) + 0.5)  # 50 … 0°N, north first
    classes = np.zeros((50, 100), np.uint8)
    classes[10, 10] = 8
    classes[10, 11] = 2
    down = resample_to_frame(classes, lons, lats, (0.0, 100.0, 0.0, 50.0), 25, 25)
    assert down.shape == (25, 25) and down[5, 2] == 8 and down.sum() == 8
    up = resample_to_frame(classes, lons, lats, (0.0, 100.0, 0.0, 50.0), 400, 200)
    assert up.shape == (200, 400)
    assert (up == 8).sum() == 16 and (up == 2).sum() == 16   # 4 × 4 per source pixel, no gaps


def test_render_radar_map_png(tmp_path):
    lons = np.linspace(120, 150, 600)
    lats = np.linspace(48, 22, 520)
    classes = np.zeros((520, 600), np.uint8)
    classes[200:260, 300:340] = 4
    from datetime import UTC, datetime
    png = render_radar_map(classes, lons, lats, valid=datetime(2026, 10, 13, 7, 30, tzinfo=UTC),
                           base=datetime(2026, 10, 13, 7, 0, tzinfo=UTC),
                           attribution="出典: 気象庁ホームページ", note="test", dpi=60)
    im = Image.open(io.BytesIO(png))
    assert im.format == "PNG" and im.size == (480, 516)
    assert "2026-10-13T07:30Z" in im.info["Description"]
