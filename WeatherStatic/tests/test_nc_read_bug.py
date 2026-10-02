#!/usr/bin/env python3
"""NetCDF の「実寸越え 2 次元読み」バグの再現と、normalize_extents の検証。

unlimited 次元 2 つの変数は、別の変数が次元を先に伸ばすと
「見かけの形 < 自分の書込み済み実寸」になる。その状態で実寸を越える
2 次元読み（var[:] や [:, j0:j1]）をすると、行の詰め直しを誤った
ずれた配列＋未初期化メモリの中身が返る。
netCDF4-python 1.6.5〜1.7.4 × libnetcdf 4.9.2〜4.10.1 で確認（2026-10-02）。
1 行・1 列の読みと、実寸内に収まる読みは正しい。

ncstore.normalize_extents が実寸を次元に揃えたあとは、どの読み方でも
正しい値が返ることを確かめる。NcStore.close() が毎回これを呼ぶので、
書込みを通った store はこのバグを踏まない、が守るべき不変条件。
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import netCDF4 as nc

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
from weatherlib.ncstore import normalize_extents  # noqa: E402

FILL = np.int16(-32768)


def build(path: Path):
    """b の実寸 (3,8) < 次元 (5,10) の状態を作る（a が次元を先に伸ばす）。"""
    ds = nc.Dataset(path, "w", format="NETCDF4")
    ds.createDimension("s", None)
    ds.createDimension("d", None)
    for name in ("a", "b"):
        ds.createVariable(name, "i2", ("s", "d"), fill_value=FILL,
                          zlib=True, chunksizes=(4, 5))
    b = ds["b"]
    for r in range(3):
        b[r, 0:8] = np.arange(100 * r, 100 * r + 8, dtype=np.int16)
    ds["a"][4, 0:10] = np.arange(10, dtype=np.int16)
    ds.close()


def expected() -> np.ndarray:
    full = np.full((5, 10), FILL, dtype=np.int16)
    for r in range(3):
        full[r, 0:8] = np.arange(100 * r, 100 * r + 8)
    return full


def check_reads(path: Path, label: str) -> bool:
    ds = nc.Dataset(path)
    ds.set_auto_mask(False)
    b = ds["b"]
    want = expected()
    ok = True
    for name, got, exp in (
        ("全読み [:]", b[:], want),
        ("日付方向越え [0:3, 6:10]", b[0:3, 6:10], want[0:3, 6:10]),
        ("地点方向越え [0:5, 0:4]", b[0:5, 0:4], want[0:5, 0:4]),
        ("行読み [1,:]", b[1, :], want[1]),
        ("列読み [:,3]", b[:, 3], want[:, 3]),
    ):
        same = np.array_equal(got, exp)
        ok &= same
        print(f"  {label} {name}: {'OK' if same else 'NG'}")
    ds.close()
    return ok


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "repro.nc"
        build(p)

        print("1. 実寸が次元より小さいままの読み（ライブラリのバグ確認）")
        raw_ok = check_reads(p, "素のまま")
        if raw_ok:
            print("  → このライブラリではバグが出ない（修正された版かも）。"
                  "normalize は引き続き無害。")
        else:
            print("  → バグ再現（ずれ・未初期化メモリ）。normalize が必須。")

        print("2. normalize_extents 後は全パターン正しいこと")
        ds = nc.Dataset(p, "a")
        ds.set_auto_mask(False)
        n = normalize_extents(ds)
        ds.close()
        print(f"  実寸を揃えた変数: {n}")
        if not check_reads(p, "揃えた後"):
            print("✗ normalize_extents 後も読みが壊れています")
            return 1

        print("3. normalize が値を変えないこと（角の書き戻しだけ）")
        ds = nc.Dataset(p)
        ds.set_auto_mask(False)
        same = np.array_equal(ds["b"][:], expected())
        ds.close()
        if not same:
            print("✗ normalize が値を変えました")
            return 1

    print("✓ ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
