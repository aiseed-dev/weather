#!/usr/bin/env python3
"""libnetcdf の「実寸越え読み」バグの再現と、ストア側の防御の検証。

unlimited 次元 2 つの変数は、別の変数が次元を先に伸ばすと
「見かけの形（次元）＞ 自分の書込み済み実寸」になる。その状態で実寸を
越える領域を読むと、libnetcdf の nc_get_vara が詰め直しと fill 埋めを誤り、
ずれた配列と手つかずのバッファ（未初期化メモリ）を rc=0 で返す。
libnetcdf 4.9.2〜4.10.1 で確認（2026-10-02〜03）。ctypes で C を直接呼んでも
再現するので、netCDF4-python の問題ではない。ncdump が正しく見えるのは
行単位で読むから。

総当たりで確かめた安全条件:
  - 1 セル読み・1 行読み（1×n）は常に正しい
  - 列読み（n×1）は、列が日付方向の実寸より外にあると壊れる
  - 2 次元読みは実寸を越えると壊れる
  - 実寸＝次元なら全て正しい

このバグは実害を出した。update_daily_extreme は列を読んで書き戻すので、
同じ実行の中で先に別の変数が新しい日を書いて次元を伸ばすと、ゴミを読んで
列ごと書き戻す（2026-08-26〜29 の tmax/tmin）。3. がその回帰テスト。
"""
import itertools
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import netCDF4 as nc

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
from weatherlib.ncstore import FILL_B, NcStore, date_index, normalize_extents  # noqa: E402
from weatherlib.store import open_store  # noqa: E402

FILL = np.int16(-32768)
EXT, DIMS = (3, 8), (5, 10)        # b の実寸 < 次元


def build(path: Path):
    ds = nc.Dataset(path, "w", format="NETCDF4")
    ds.createDimension("s", None)
    ds.createDimension("d", None)
    for name in ("a", "b"):
        ds.createVariable(name, "i2", ("s", "d"), fill_value=FILL, chunksizes=(4, 5))
    for r in range(EXT[0]):
        ds["b"][r, 0:EXT[1]] = np.arange(100 * r, 100 * r + EXT[1], dtype=np.int16)
    ds["a"][DIMS[0] - 1, DIMS[1] - 1] = 1      # a が次元を先に伸ばす
    ds.close()


def expected() -> np.ndarray:
    full = np.full(DIMS, FILL, dtype=np.int16)
    for r in range(EXT[0]):
        full[r, 0:EXT[1]] = np.arange(100 * r, 100 * r + EXT[1])
    return full


def sweep(path: Path) -> dict[str, list[int]]:
    """全矩形を読み、種類別の [NG 数, 総数] を返す。"""
    ds = nc.Dataset(path)
    ds.set_auto_mask(False)
    b, want = ds["b"], expected()
    out = {"1セル": [0, 0], "行(1×n)": [0, 0], "列(n×1)": [0, 0], "2次元": [0, 0]}
    for s0, s1 in itertools.combinations(range(DIMS[0] + 1), 2):
        for d0, d1 in itertools.combinations(range(DIMS[1] + 1), 2):
            kind = ("1セル" if s1 - s0 == 1 and d1 - d0 == 1 else
                    "行(1×n)" if s1 - s0 == 1 else
                    "列(n×1)" if d1 - d0 == 1 else "2次元")
            out[kind][1] += 1
            if not np.array_equal(b[s0:s1, d0:d1], want[s0:s1, d0:d1]):
                out[kind][0] += 1
    ds.close()
    return out


def show(res) -> str:
    return " / ".join(f"{k} {ng}/{tot} NG" for k, (ng, tot) in res.items())


def store_regression(td: Path) -> bool:
    """実コードで: 新しい日の tmax→tmin を同じ実行で書いても、CSV に無い行を汚さない。"""
    conn = open_store(td / "weather.sqlite")
    n = 1300
    codes = [f"{10000 + i}" for i in range(n)]
    d1, d2 = date(2026, 8, 26), date(2026, 8, 27)

    ncs = NcStore(td / "observations.nc", conn)
    everyone = {c: (250 + i % 50, "13:00", 8) for i, c in enumerate(codes)}
    ncs.update_daily_extreme(d1, "tmax", everyone)
    ncs.update_daily_extreme(d1, "tmin", everyone)
    conn.commit()
    ncs.close()

    ncs = NcStore(td / "observations.nc", conn)
    part = {c: (300, "14:00", 8) for c in codes[:900]}     # 残り 400 行は欠測のはず
    junk = [np.random.randint(-30000, 30000, 4000, dtype=np.int16) for _ in range(200)]
    del junk                                               # ヒープを汚しておく
    ncs.update_daily_extreme(d2, "tmax", part)
    ncs.update_daily_extreme(d2, "tmin", part)
    conn.commit()
    ncs.close()
    conn.close()

    ds = nc.Dataset(td / "observations.nc")
    ds.set_auto_mask(False)
    j = date_index(d2)
    ok = True
    for var in ("tmax", "tmin"):
        dirty = 0
        for r in range(900, n):                            # 1 セル読みは常に正しい
            dirty += (ds[var][r, j] != FILL) or (ds[f"{var}_q"][r, j] != FILL_B) \
                or (ds[f"{var}_minutes"][r, j] != FILL)
        print(f"  {var}: CSV に無い 400 行のうち汚れた行 {dirty}")
        ok &= dirty == 0
    ds.close()
    return ok


def main() -> int:
    td = Path(tempfile.mkdtemp())
    try:
        p = td / "repro.nc"
        build(p)

        print(f"1. 実寸{EXT} < 次元{DIMS} のままの読み（ライブラリのバグ確認）")
        raw = sweep(p)
        print("  " + show(raw))
        if raw["1セル"][0] or raw["行(1×n)"][0]:
            print("✗ 1 セル・1 行読みまで壊れています（防御の前提が崩れる）")
            return 1
        if not any(ng for ng, _ in raw.values()):
            print("  → このライブラリではバグが出ない（修正された版かも）。防御は無害。")

        print("2. normalize_extents 後は全矩形が正しく、値も変わらないこと")
        ds = nc.Dataset(p, "a")
        ds.set_auto_mask(False)
        normalize_extents(ds)
        ds.close()
        fixed = sweep(p)
        print("  " + show(fixed))
        if any(ng for ng, _ in fixed.values()):
            print("✗ normalize_extents 後も読みが壊れています")
            return 1

        print("3. NcStore: 同じ実行で新しい日を続けて書いても列を汚さないこと")
        if not store_regression(td):
            print("✗ update_daily_extreme が CSV に無い行を汚しました")
            return 1
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("✓ ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
