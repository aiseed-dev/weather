# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""GPU ビューアへ 1 ステップ分を渡すための書き出し。

なぜこれが要るか
----------------
今の描画経路は「領域が変わるたびに PNG を作り直して UI へ渡す」形で、
パン・ズームのたびに Python と描画層の間を往復する。往復が毎フレーム
起きることが遅さの原因で、matplotlib を外した後も残っている
(`_fast.py` は既に numpy + PIL の C 実装だけで動いている)。

GPU ビューアはグリッドをテクスチャに載せ、表示範囲を変えるだけで
パン・ズームする。**境界を越えるのは 1 ステップにつき 1 回**になり、
毎フレームの往復が消える。

役割分担
--------
Python  … 取得・GRIB 復号・投影・切り出し・配色の決定(ここまで)
Rust    … テクスチャ化・パン・ズーム・画面

渡すもの
--------
値の格子(float32)と 256 段の LUT と値域だけ。**画像は作らない**。
色付けはシェーダが LUT を引いて行う。値そのものを渡すので、
ビューア側で値の読み取り(カーソル位置の実数値)もできる。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from aiseed_weather.figures.regions import Region

MAGIC = b"AWGP"          # AIseed Weather GPU Payload
VERSION = 1


def write_payload(
    path: Path,
    data: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    lut: np.ndarray,
    *,
    vmin: float,
    vmax: float,
    meta: dict,
) -> Path:
    """1 ステップ分をビューアが読める形で書き出す。

    形式は「小さなヘッダ + 生の配列」。JSON に数値配列を入れると
    100 万点で数十 MB になるうえ解析に時間がかかるので、**メタ情報だけ
    JSON、格子は生バイト**にする。1440x721 の float32 で約 4MB。

    レイアウト:
        magic(4) version(u32) header_len(u32) header_json(header_len)
        data(f32 * ny * nx)  行 0 が北端(_fast.crop_grid と同じ向き)
        lons(f32 * nx)
        lats(f32 * ny)
        lut(u8 * 256 * 3)
    """
    data = np.ascontiguousarray(data, dtype=np.float32)
    if data.ndim != 2:
        raise ValueError(f"格子は 2 次元でなければならない: {data.shape}")
    ny, nx = data.shape
    lons = np.ascontiguousarray(longitudes, dtype=np.float32)
    lats = np.ascontiguousarray(latitudes, dtype=np.float32)
    if lons.size != nx or lats.size != ny:
        raise ValueError(
            f"座標軸が格子と合わない: data={data.shape} lons={lons.size} lats={lats.size}")
    lut = np.ascontiguousarray(lut, dtype=np.uint8)
    if lut.shape != (256, 3):
        raise ValueError(f"LUT は (256, 3) uint8 でなければならない: {lut.shape}")

    header = json.dumps({
        "nx": nx, "ny": ny, "vmin": float(vmin), "vmax": float(vmax),
        # 値が欠測の格子点。NaN で渡すので、ビューアは透明にする
        "nan_is_missing": True,
        **meta,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<II", VERSION, len(header)))
        f.write(header)
        f.write(data.tobytes())
        f.write(lons.tobytes())
        f.write(lats.tobytes())
        f.write(lut.tobytes())
    tmp.replace(path)                     # 読み手が途中の物を掴まないように
    return path


def read_payload(path: Path) -> dict:
    """write_payload の逆。試験と、Python 側から中身を確かめる用。"""
    raw = path.read_bytes()
    if raw[:4] != MAGIC:
        raise ValueError("形式が違う（AWGP ではない）")
    version, hlen = struct.unpack_from("<II", raw, 4)
    if version != VERSION:
        raise ValueError(f"版が違う: {version}")
    off = 12
    head = json.loads(raw[off:off + hlen])
    off += hlen
    nx, ny = head["nx"], head["ny"]
    data = np.frombuffer(raw, dtype=np.float32, count=ny * nx, offset=off).reshape(ny, nx)
    off += ny * nx * 4
    lons = np.frombuffer(raw, dtype=np.float32, count=nx, offset=off)
    off += nx * 4
    lats = np.frombuffer(raw, dtype=np.float32, count=ny, offset=off)
    off += ny * 4
    lut = np.frombuffer(raw, dtype=np.uint8, count=256 * 3, offset=off).reshape(256, 3)
    return {"header": head, "data": data, "lons": lons, "lats": lats, "lut": lut}
