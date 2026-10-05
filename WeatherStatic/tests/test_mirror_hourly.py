#!/usr/bin/env python3
"""10 分値の半月ファイルから 1 時間値（毎正時）を作る処理（fetch_amedas_mirror）。

確かめること
  1. 時刻は毎正時（分が 0）だけになる
  2. 値・品質・倍率・単位は 10 分値のその時刻と同じ（素通し）
  3. 毎正時にしか無い weather は 1 時間値に全部残る
  4. 作った後は作り直さない。10 分値が新しくなれば作り直す
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import netCDF4

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
import fetch_amedas_mirror as m  # noqa: E402


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'OK' if cond else 'NG'} {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        m.OUT, m.MAP = root, root / "map"
        m.MAP.mkdir()
        # 2026-10-01 の 10:00〜12:50（18 スロット）、2 地点
        paths = []
        for k in range(18):
            hh, mm = 10 + k // 6, (k % 6) * 10
            name = f"20261001{hh:02d}{mm:02d}00"
            snap = {"44132": {"temp": [20.0 + k / 10, 0], "pressure": [1013.4, 0]},
                    "11001": {"temp": [10.0 + k / 10, 0]}}
            if mm == 0:
                snap["44132"]["weather"] = [100 + hh, 0]
            (m.MAP / f"{name}.json").write_text(json.dumps(snap))
            paths.append(m.MAP / f"{name}.json")
        src = m.period_path((2026, 10, 1))
        m.write_period_netcdf((2026, 10, 1), paths, src)

        print("1〜3. 1 時間値を作る")
        check("作った数", m.ensure_hourly() == 1)
        dst = m.hourly_path(src)
        with netCDF4.Dataset(src) as a, netCDF4.Dataset(dst) as b:
            ta, tb = a["time"][:], b["time"][:]
            check("時刻は毎正時だけ（10:00・11:00・12:00）",
                  list(tb) == [600, 660, 720] and all(t % 60 == 0 for t in tb))
            check("time の単位は同じ", a["time"].units == b["time"].units)
            idx = [list(ta).index(t) for t in tb]
            same = all(np.array_equal(np.asarray(a[v][:, idx]), np.asarray(b[v][:]))
                       for v in ("temp", "temp_q", "pressure", "weather"))
            check("値・品質がそのまま（物理量に直しても同じ）", same)
            check("倍率と単位が同じ", b["temp"].scale_factor == a["temp"].scale_factor
                  and b["pressure"].units == a["pressure"].units)
            i = list(b["station_id"][:]).index("44132")
            check("weather は正時の 3 時刻すべてに残る",
                  [int(x) for x in b["weather"][i, :]] == [110, 111, 112])
            check("気温 10:00 → 20.0 ℃（倍率込みで読める）",
                  abs(float(b["temp"][i, 0]) - 20.0) < 1e-6)
            check("由来を属性に残す", "毎正時" in b.derived_from and b.slot_minutes == 60)

        print("4. 作り直し")
        check("同じなら作り直さない", m.ensure_hourly() == 0)
        future = time.time() + 5
        os.utime(src, (future, future))
        check("10 分値が新しくなれば作り直す", m.ensure_hourly() == 1)

    print("✓ ok" if ok else "✗ 1 時間値の作り方が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
