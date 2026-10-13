# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""GPU 描画の試作用データを作る。

ECMWF Open Data（GCS ミラー）の bulk GRIB から、2m 気温と 10m 風（u・v）の通報だけを
.index を見て Range で取り、日本の周りを切り出して float32 の素の配列にする。
試作のアプリは、この配列をそのまま GPU 描画の部品（flet-gpufield）へ渡す。

    python prepare_data.py --run 2026101212 --steps 0-72/3

出力（data/{run}/）:
    meta.json         格子（西端・北端の格子点、間隔、点数）、ステップ、出典
    {step:03d}.bin    float32 little-endian、[t2m(℃), u10, v10] × ny × nx（北から南の行順）
    coast.bin         海岸線。float32 の (経度, 緯度) の並び、線の切れ目は NaN
    lut_{層}.bin      配色。256 段 × RGBA8。アプリの図と同じ配色（figures/_chart_specs.py）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))
from aiseed_weather.services.forecast_service import _bulk_url  # noqa: E402

UA = {"User-Agent": "aiseed-weather-gpu-prototype"}
WANT = ("2t", "10u", "10v")
# 日本の周り（0.25° 格子の点に合わせる）
LON_W, LON_E, LAT_N, LAT_S = 100.0, 170.0, 60.0, 10.0
ATTRIBUTION = "Data: ECMWF Open Data (CC-BY-4.0)"


def get(url: str, rng: "tuple[int, int] | None" = None, retries: int = 5) -> bytes:
    headers = dict(UA)
    if rng:
        headers["Range"] = f"bytes={rng[0]}-{rng[1]}"
    for k in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                data = r.read()
            if rng is None or len(data) == rng[1] - rng[0] + 1:
                return data
        except OSError:
            pass
        time.sleep(1 + k)
    raise RuntimeError(f"取れない: {url} {rng}")


def fetch_step(run, step: int, out: Path) -> Path:
    """必要な 3 通報だけを取って 1 本の GRIB にする。"""
    path = out / f"{step:03d}.grib2"
    if path.exists():
        return path
    url = _bulk_url("google", run, step)
    entries = [json.loads(line) for line in get(url.replace(".grib2", ".index")).decode().splitlines()]
    msgs = sorted((e for e in entries if e.get("levtype") == "sfc" and e["param"] in WANT),
                  key=lambda e: e["_offset"])
    blobs = [get(url, (e["_offset"], e["_offset"] + e["_length"] - 1)) for e in msgs]
    path.write_bytes(b"".join(blobs))
    return path


def decode(path: Path) -> "dict[str, np.ndarray]":
    """eccodes で 3 通報を (721, 1440) の配列に。"""
    import eccodes
    out = {}
    with path.open("rb") as f:
        while (h := eccodes.codes_grib_new_from_file(f)) is not None:
            try:
                name = eccodes.codes_get(h, "shortName")
                ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
                out[name] = eccodes.codes_get_values(h).reshape(nj, ni)
            finally:
                eccodes.codes_release(h)
    return out


def crop(a: np.ndarray) -> np.ndarray:
    """全球 0.25°（北 90 → 南 -90、経度 0 → 359.75）から日本の周りを切る。"""
    j0, j1 = int((90 - LAT_N) / 0.25), int((90 - LAT_S) / 0.25) + 1
    i0, i1 = int(LON_W / 0.25), int(LON_E / 0.25) + 1
    return a[j0:j1, i0:i1]


def write_coast(out: Path) -> int:
    """Natural Earth 10m の海岸線を範囲で切り、0.02° 程度に間引く。"""
    import shapefile
    from shapely.geometry import LineString, box
    shp = Path.home() / ".local/share/cartopy/shapefiles/natural_earth/physical/ne_10m_coastline.shp"
    area = box(LON_W - 1, LAT_S - 1, LON_E + 1, LAT_N + 1)
    parts = []
    for shape in shapefile.Reader(str(shp)).shapes():
        pts = shape.points
        idx = list(shape.parts) + [len(pts)]
        for a, b in zip(idx[:-1], idx[1:]):
            if b - a < 2:
                continue
            line = LineString(pts[a:b])
            if not line.intersects(area):
                continue
            clipped = line.intersection(area).simplify(0.02)
            geoms = getattr(clipped, "geoms", [clipped])
            for g in geoms:
                if g.geom_type == "LineString" and len(g.coords) >= 2:
                    parts.append(np.asarray(g.coords, dtype=np.float32))
                    parts.append(np.array([[np.nan, np.nan]], dtype=np.float32))
    arr = np.concatenate(parts) if parts else np.zeros((0, 2), np.float32)
    (out / "coast.bin").write_bytes(arr.astype("<f4").tobytes())
    return len(arr)


def write_luts(out: Path) -> dict:
    """アプリの図と同じ配色（T2M・WIND10M の anchors）を 256 段の表にする。"""
    from aiseed_weather.figures._chart_specs import T2M, WIND10M
    from aiseed_weather.figures._palette import build_continuous_lut
    layers = {}
    for key, spec, label in (("t2m", T2M, "2m 気温 (℃)"), ("wind", WIND10M, "10m 風速 (m/s)")):
        rgb = build_continuous_lut(spec.anchors, spec.vmin, spec.vmax)
        rgba = np.concatenate([rgb, np.full((256, 1), 255, np.uint8)], axis=1)
        (out / f"lut_{key}.bin").write_bytes(rgba.tobytes())
        layers[key] = {"vmin": spec.vmin, "vmax": spec.vmax, "lut": f"lut_{key}.bin", "label": label,
                       "short": label.split()[1]}
    return layers


def parse_steps(text: str) -> list[int]:
    if "-" in text:
        rng, _, every = text.partition("/")
        a, b = (int(x) for x in rng.split("-"))
        return list(range(a, b + 1, int(every or 3)))
    return [int(s) for s in text.split(",")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="YYYYMMDDHH")
    ap.add_argument("--steps", default="0-72/3")
    args = ap.parse_args()
    from datetime import datetime, timezone
    run = datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    out = HERE / "data" / args.run
    out.mkdir(parents=True, exist_ok=True)
    steps = parse_steps(args.steps)
    t_fetch = t_decode = 0.0
    shape = None
    for step in steps:
        t = time.time()
        g = fetch_step(run, step, out)
        t_fetch += time.time() - t
        t = time.time()
        f = decode(g)
        t_decode += time.time() - t
        t2m, u, v = crop(f["2t"]) - 273.15, crop(f["10u"]), crop(f["10v"])
        shape = t2m.shape
        (out / f"{step:03d}.bin").write_bytes(np.stack([t2m, u, v]).astype("<f4").tobytes())
        print(f"step {step:3d}h: {shape[1]}×{shape[0]}  気温 {t2m.min():.1f}〜{t2m.max():.1f}℃  "
              f"風速最大 {np.hypot(u, v).max():.1f} m/s", flush=True)
    n_coast = write_coast(out)
    layers = write_luts(out)
    meta = {"run": f"{run:%Y-%m-%dT%H}Z", "steps": steps, "attribution": ATTRIBUTION, "layers": layers,
            "lon0": LON_W, "lat0": LAT_N, "dlon": 0.25, "dlat": 0.25,
            "nx": shape[1], "ny": shape[0], "channels": ["t2m", "u10", "v10"]}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"取得 {t_fetch:.1f} 秒 / eccodes で decode {t_decode:.2f} 秒（{len(steps)} ステップ）/ 海岸線 {n_coast} 点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
