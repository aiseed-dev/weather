# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
"""services/gpu_radar: nowcast frames for the GPU map."""

import numpy as np

from aiseed_weather.figures._japan_map import AREAS, EXTENT, JMA_PRECIP_COLOURS, area, tile_km
from aiseed_weather.services import gpu_radar as gr


def test_grids_match_the_tile_resolution():
    g = gr.area_grid(area("japan"))
    assert (g.nx, g.ny) == (302, 306)
    assert abs(g.dlon - 360 / (256 * 16)) < 0.001             # one zoom-4 tile pixel
    w = gr.area_grid(area("west"))
    assert abs(w.dlon - 360 / (256 * 64)) < 0.0005            # one zoom-6 tile pixel
    assert g.lon0 == EXTENT[0] + g.dlon / 2 and g.lat0 == EXTENT[3] - g.dlat / 2


def test_areas_stay_inside_the_japan_frame():
    for a in AREAS:
        lon0, lon1, lat0, lat1 = a.extent
        assert EXTENT[0] <= lon0 < lon1 <= EXTENT[1] and EXTENT[2] <= lat0 < lat1 <= EXTENT[3]
        assert a.figure_zoom % 2 == 0 and a.moving_zoom % 2 == 0
    assert area("no such area") is AREAS[0]
    assert round(tile_km(4, 35.0)) == 8 and round(tile_km(8, 35.0), 1) == 0.5


def test_pack_frame_stores_the_class_and_marks_no_data():
    grid = gr.RadarGrid((0.0, 10.0, 0.0, 10.0), nx=10, ny=10)
    lons = np.arange(10) + 0.5
    lats = 10.0 - (np.arange(10) + 0.5)
    classes = (np.arange(100) % 9).astype(np.uint8).reshape(10, 10)
    # No data everywhere except the observed hole x 0 … 5, y 0 … 10.
    world = np.array([[-45, -85], [315, -85], [315, 85], [-45, 85], [-45, -85]], float)
    hole = np.array([[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]], float)
    rgba = np.frombuffer(gr.pack_frame(classes, lons, lats, grid, ((world, hole),)),
                         np.uint8).reshape(10, 10, 4)
    level = np.rint((rgba[..., 0].astype(int) * 256 + rgba[..., 1]) / 65535 * gr.N_LEVELS)
    assert np.array_equal(level[:, :5], classes[:, :5])
    assert (level[:, 5:] == gr.NO_DATA).all()
    assert (rgba[..., 3] == 255).all()


def test_lut_draws_jma_colours_and_the_no_data_veil():
    lut = np.frombuffer(gr.lut_rgba(), np.uint8).reshape(256, 4)
    assert lut[0, 3] == 0
    for k, colour in enumerate(JMA_PRECIP_COLOURS, start=1):
        entry = lut[int(round(k / gr.N_LEVELS * 255))]
        assert "#%02x%02x%02x" % tuple(entry[:3]) == colour and entry[3] == 255
    assert 0 < lut[255, 3] < 255                       # the veil lets the map show through
    assert gr.CLASS_LABELS[0] == "" and gr.CLASS_LABELS[8] == "降水強度 80〜 mm/h"
    assert gr.CLASS_LABELS[gr.NO_DATA] == "観測範囲外・欠測"


def test_land_mask_and_coastline():
    g = gr.area_grid(area("japan"))
    land = np.frombuffer(gr.land_mask(g), np.uint8).reshape(g.ny, g.nx)

    def at(lat, lon):
        return land[int((g.extent[3] - lat) / g.dlat), int((lon - g.extent[0]) / g.dlon)]

    assert at(36.0, 138.0) == 1          # central Honshu
    assert at(30.0, 145.0) == 0          # Pacific
    coast = np.frombuffer(gr.coastline(), "<f4").reshape(-1, 2)
    assert len(coast) > 5000 and np.isnan(coast[:, 0]).any()
