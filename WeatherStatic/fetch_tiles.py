#!/usr/bin/env python3
"""世界天気タイル生成（DESIGN.md「世界天気タイル」章。ユーザー決定: IFS・1日4回）。

ECMWF Open Data IFS 0.25° の 2m 気温を .index + HTTP Range で 1 フィールドだけ
取得し（約 0.5MB/時刻、丸ごとなら 150MB）、Web Mercator XYZ タイル z0〜4 に描く。

  出力: public/tiles/t2m/{step}h/{z}/{x}/{y}.png   (256px、341 タイル/時刻)
        public/tiles/latest.json                    (ラン・時刻・凡例)

配色はここが正典（/WorldTime/Map の点表示は latest.json の legend を採用する）。
時刻は 解析 0h + 24/48/72h。00/06/12/18Z の 4 ラン運用（06/18Z も 90h まで
公開されるため 72h まで取れる）。

usage:
    .venv/bin/python fetch_tiles.py                # 最新ラン
    .venv/bin/python fetch_tiles.py --steps 0      # 動作確認
"""
from __future__ import annotations

import argparse
import io
import json
import math
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / "public"
UA = {"User-Agent": "aiseed-weather-tiles (+https://github.com/aiseed-dev/weather)"}
ECMWF = "https://storage.googleapis.com/ecmwf-open-data"
STEPS = [0, 24, 48, 72]
ZMAX = 4
TILE = 256

# 気温の配色（℃ → RGB）。正典。凡例として latest.json にも書き出す
ANCHORS = [(-40, (140, 60, 180)), (-25, (60, 60, 200)), (-10, (70, 130, 230)),
           (0, (170, 215, 250)), (10, (140, 210, 150)), (20, (250, 220, 100)),
           (30, (245, 140, 60)), (40, (200, 30, 40)), (45, (120, 10, 30))]


def build_lut() -> np.ndarray:
    """-50..+50℃ を 0.25℃ 刻みで引ける (401,3) の LUT。"""
    xs = np.linspace(-50, 50, 401)
    pts = np.array([a[0] for a in ANCHORS], dtype=float)
    lut = np.stack([np.interp(xs, pts, [a[1][c] for a in ANCHORS])
                    for c in range(3)], axis=1)
    return lut.astype(np.uint8)


def http_get(url: str, rng=None) -> bytes:
    h = dict(UA)
    if rng:
        h["Range"] = f"bytes={rng[0]}-{rng[1]}"
    req = urllib.request.Request(url, headers=h)
    for attempt, delay in ((1, 2), (2, 4), (3, None)):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except (urllib.error.URLError, OSError):
            if delay is None:
                raise
            time.sleep(delay)


def latest_run() -> datetime:
    """72h の index がある最新ラン（6 時間刻みでさかのぼる）。"""
    cand = datetime.now(timezone.utc) - timedelta(hours=7)
    cand = cand.replace(hour=(cand.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for _ in range(8):
        try:
            http_get(_url(cand, 72) + ".index", rng=(0, 0))
            return cand
        except urllib.error.HTTPError:
            cand -= timedelta(hours=6)
    raise RuntimeError("完全公開ランが見つからない")


def _url(run: datetime, step: int) -> str:
    # 拡張子なしの共通部。GRIB は +".grib2"、index は +".index"
    # （.grib2.index ではない点に注意）
    return (f"{ECMWF}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/"
            f"{run:%Y%m%d}{run:%H}0000-{step}h-oper-fc")


def fetch_t2m(run: datetime, step: int) -> np.ndarray:
    """2t 1 フィールドを Range 取得して ℃ の (721,1440) を返す。"""
    idx = http_get(_url(run, step) + ".index").decode()
    ent = next(json.loads(l) for l in idx.splitlines()
               if '"2t"' in l and json.loads(l).get("param") == "2t")
    buf = http_get(_url(run, step) + ".grib2", (ent["_offset"], ent["_offset"] + ent["_length"] - 1))
    import cfgrib
    with tempfile.NamedTemporaryFile(suffix=".grib2") as f:
        f.write(buf)
        f.flush()
        ds = cfgrib.open_datasets(f.name)[0]
        # lat 90→-90, lon 0→359.75
        return (ds["t2m"].values.astype(np.float32) - 273.15)


def render_tiles(field: np.ndarray, out_dir: Path, lut: np.ndarray) -> int:
    """(721,1440) の ℃ 格子を z0..ZMAX の XYZ タイルへ。最近傍サンプリング。"""
    n_tiles = 0
    for z in range(ZMAX + 1):
        n = 2 ** z
        px_global = TILE * n
        # 経度は全タイル共通の列 → z ごとに 1 回だけ計算
        lon_idx = ((np.arange(px_global) + 0.5) / px_global * 1440).astype(int) % 1440
        # 緯度（Web Mercator 逆変換）も z ごとに 1 回
        ypix = np.arange(px_global) + 0.5
        lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * ypix / px_global))))
        lat_idx = np.clip(np.round((90.0 - lat) / 0.25).astype(int), 0, 720)
        for x in range(n):
            cols = lon_idx[x * TILE:(x + 1) * TILE]
            for y in range(n):
                rows = lat_idx[y * TILE:(y + 1) * TILE]
                vals = field[np.ix_(rows, cols)]
                ci = np.clip(np.round((vals + 50.0) * 4).astype(int), 0, 400)
                rgb = lut[ci]
                p = out_dir / str(z) / str(x)
                p.mkdir(parents=True, exist_ok=True)
                buf = io.BytesIO()
                Image.fromarray(rgb, "RGB").save(buf, "PNG", compress_level=6)
                (p / f"{y}.png").write_bytes(buf.getvalue())
                n_tiles += 1
    return n_tiles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="all", help="'all' か カンマ区切り（例 0,24）")
    ap.add_argument("--run", help="YYYYMMDDHH")
    args = ap.parse_args()
    steps = STEPS if args.steps == "all" else [int(s) for s in args.steps.split(",")]

    run = (datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
           if args.run else latest_run())
    print(f"[tiles] ラン: {run:%Y-%m-%d %H}Z / steps {steps}")
    lut = build_lut()
    total = 0
    for step in steps:
        t0 = time.monotonic()
        field = fetch_t2m(run, step)
        n = render_tiles(field, PUBLIC / "tiles" / "t2m" / f"{step}h", lut)
        total += n
        print(f"[tiles]   {step:3d}h: {n} タイル ({time.monotonic() - t0:.1f}s)")

    meta = {
        "run": f"{run:%Y-%m-%dT%H}Z", "model": "ifs-0p25-oper",
        "elements": {"t2m": {"steps": steps, "unit": "°C",
                             "legend": [{"value": v, "rgb": list(c)} for v, c in ANCHORS]}},
        "zoom": [0, ZMAX], "tile_url": "/tiles/{element}/{step}h/{z}/{x}/{y}.png",
        "attribution": "Data: ECMWF open data (CC BY 4.0)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (PUBLIC / "tiles" / "latest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[tiles] 完了: {total} タイル + latest.json")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
