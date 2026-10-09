# SPDX-License-Identifier: AGPL-3.0-or-later
"""日本 EEZ 空白マスク（figures/_blank.py）の回帰テスト。

海外向け天気図は日本の EEZ を必ず空白にする（worldtime 設計書 K13）。
ここが壊れると空白なしの図が公開されうるので、マスクの存在と
描画結果への反映、そして安全側の例外を確かめる。
"""
from __future__ import annotations

import io

import numpy as np
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("PIL")
pytest.importorskip("scipy")
pytest.importorskip("contourpy")

from aiseed_weather.figures import _blank  # noqa: E402
from aiseed_weather.figures.regions import ASIA, EUROPE, JAPAN, ARCTIC, PRESETS  # noqa: E402


def _synthetic_ds() -> "xr.Dataset":
    lats = np.arange(90, -90.25, -0.25, dtype=np.float32)
    lons = np.arange(0, 360, 0.25, dtype=np.float32)
    lon, lat = np.meshgrid(lons, lats)
    t2m = (300 - 0.5 * np.abs(lat) + 5 * np.sin(np.radians(lon * 3))).astype(np.float32)
    return xr.Dataset({"t2m": (("latitude", "longitude"), t2m)},
                      coords={"latitude": lats, "longitude": lons})


def _render(region, blank: str | None):
    from PIL import Image
    from aiseed_weather.figures._layered_renderer import render
    from aiseed_weather.figures.t2m_chart import T2M
    ds = _synthetic_ds()
    if blank is None:
        png = render(T2M, ds, region=region, run_id="test")
    else:
        with _blank.blanking(blank):
            png = render(T2M, ds, region=region, run_id="test")
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


def test_every_preset_has_a_jp_eez_mask():
    for region in PRESETS:
        assert _blank.has_mask("jp_eez", region.key), region.key


def test_mask_covers_japan_and_not_europe():
    masks = _blank._MASKS
    assert masks["jp_eez__japan"].sum() > 5000
    assert masks["jp_eez__asia"].sum() > 5000
    assert masks["jp_eez__arctic"].sum() > 5000      # 北極図の縁に日本が入る
    assert masks["jp_eez__europe"].sum() == 0
    assert masks["jp_eez__africa"].sum() == 0


def test_blank_is_painted_only_when_active():
    plain = _render(ASIA, None)
    blanked = _render(ASIA, "jp_eez")
    changed = (plain != blanked).any(axis=2)
    mask = _blank._MASKS["jp_eez__asia"]
    assert changed.shape == mask.shape
    # 空白セルは指定色、空白外は変わらない（海岸線が重なるセルを除く）
    assert (blanked[mask] == _blank.BLANK_RGB).all(axis=1).mean() > 0.95
    assert not changed[~mask].any()


def test_inactive_blank_changes_nothing_outside_japan():
    assert np.array_equal(_render(EUROPE, None), _render(EUROPE, "jp_eez"))


def test_unknown_blank_key_raises():
    with pytest.raises(RuntimeError):
        with _blank.blanking("no-such-blank"):
            _render(JAPAN, "no-such-blank")


def test_polar_path_is_blanked_too():
    plain = _render(ARCTIC, None)
    blanked = _render(ARCTIC, "jp_eez")
    assert (plain != blanked).any()
