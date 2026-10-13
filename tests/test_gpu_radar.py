# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
"""services/gpu_radar: nowcast frames for the GPU map."""

import numpy as np

from aiseed_weather.figures._japan_map import EXTENT, JMA_PRECIP_COLOURS
from aiseed_weather.services import gpu_radar as gr


def test_grids_match_the_tile_resolution():
    g = gr.area("japan").grid
    assert (g.nx, g.ny) == (302, 306)
    assert abs(g.dlon - 360 / (256 * 16)) < 0.001             # one zoom-4 tile pixel
    w = gr.area("west").grid
    assert abs(w.dlon - 360 / (256 * 64)) < 0.0005            # one zoom-6 tile pixel
    assert g.lon0 == EXTENT[0] + g.dlon / 2 and g.lat0 == EXTENT[3] - g.dlat / 2


def test_areas_stay_inside_the_japan_frame():
    for a in gr.AREAS:
        lon0, lon1, lat0, lat1 = a.extent
        assert EXTENT[0] <= lon0 < lon1 <= EXTENT[1] and EXTENT[2] <= lat0 < lat1 <= EXTENT[3]
        assert a.zoom % 2 == 0
    assert gr.area("no such area") is gr.AREAS[0]


def test_pack_frame_stores_the_class_as_the_value():
    grid = gr.RadarGrid((0.0, 10.0, 0.0, 10.0), nx=10, ny=10)
    lons = np.arange(10) + 0.5
    lats = 10.0 - (np.arange(10) + 0.5)
    classes = (np.arange(100) % 9).astype(np.uint8).reshape(10, 10)
    rgba = np.frombuffer(gr.pack_frame(classes, lons, lats, grid), np.uint8).reshape(10, 10, 4)
    q = (rgba[..., 0].astype(int) * 256 + rgba[..., 1]) / 65535
    assert np.array_equal(np.rint(q * gr.N_CLASSES), classes)
    assert (rgba[..., 3] == 255).all()


def test_lut_draws_jma_colours_and_leaves_no_echo_transparent():
    lut = np.frombuffer(gr.lut_rgba(), np.uint8).reshape(256, 4)
    assert lut[0, 3] == 0
    for k, colour in enumerate(JMA_PRECIP_COLOURS, start=1):
        entry = lut[int(round(k / gr.N_CLASSES * 255))]
        assert "#%02x%02x%02x" % tuple(entry[:3]) == colour and entry[3] == 255
    assert gr.CLASS_LABELS[0] == "" and gr.CLASS_LABELS[-1] == "80〜"


def test_land_mask_and_coastline():
    g = gr.area("japan").grid
    land = np.frombuffer(gr.land_mask(g), np.uint8).reshape(g.ny, g.nx)

    def at(lat, lon):
        return land[int((g.extent[3] - lat) / g.dlat), int((lon - g.extent[0]) / g.dlon)]

    assert at(36.0, 138.0) == 1          # central Honshu
    assert at(30.0, 145.0) == 0          # Pacific
    coast = np.frombuffer(gr.coastline(), "<f4").reshape(-1, 2)
    assert len(coast) > 5000 and np.isnan(coast[:, 0]).any()
