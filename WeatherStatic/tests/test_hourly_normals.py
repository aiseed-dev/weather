#!/usr/bin/env python3
"""時別の平年値の読み出し（weatherlib/hourly_normals.py）。トップの地図の平年差に使う。

確かめること
  1. 自前の時別の平年値: 毎正時はその値、間は線形補間、0 時は前日の 24 時
  2. ほかの官署: 近い都市の日変化の形を借り、日平均と日較差は自分の日別平年値
  3. 0〜24 時の 25 値（ページが補間する）と、値が無いときの None
"""
import sys
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weatherlib.hourly_normals import HourlyNormals  # noqa: E402

# 都市 A（自前）: 10/5 は 1 時 100 から 1 時間ごとに +10（24 時 330、平均 215）。10/4 の 24 時は 90
ROW = [100 + 10 * i for i in range(24)]
PREV = [90] * 24
DAILY = {  # (code, elem) -> ×10
    (1, "tavg"): 215, (1, "tmax"): 330, (1, "tmin"): 100,
    (2, "tavg"): 300, (2, "tmax"): 360, (2, "tmin"): 245,     # 日較差 11.5℃ = A の 0.5 倍
    (3, "tavg"): 100, (3, "tmax"): 150, (3, "tmin"): 50,
}


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as tmp:
        temp = {"10": {"temp": [None] * 31}}
        temp["10"]["temp"][3] = PREV
        temp["10"]["temp"][4] = ROW
        Path(tmp, "1.json").write_text(json.dumps({"daily": temp}), encoding="utf-8")
        hn = HourlyNormals(Path(tmp), lambda c, e, d: DAILY.get((c, e)) if d.month == 10 else None,
                           {1: (35.0, 139.0), 2: (36.0, 139.5), 3: (43.0, 141.0)})
        print("1. 自前の時別の平年値")
        check("毎正時はその値（13 時 = index 12）", hn.at(1, datetime(2026, 10, 5, 13, 0)) == (220, 1))
        check("間は線形補間（13:30）", hn.at(1, datetime(2026, 10, 5, 13, 30)) == (225, 1))
        check("0:30 は前日 24 時と 1 時の間", hn.at(1, datetime(2026, 10, 5, 0, 30)) == (95, 1))
        print("2. 近い都市の形を借りる")
        # 13 時の A の偏差 = 220 − 215 = 5、日較差の比 0.5 → 300 + 2.5
        check("日平均 + 偏差 × 日較差の比", hn.at(2, datetime(2026, 10, 5, 13, 0)) == (302, 1))
        # 比は 5.0/23 ≈ 0.22 → 下限 0.5
        check("日較差の比は 0.5 で止める", hn.at(3, datetime(2026, 10, 5, 13, 0)) == (102, 1))
        print("3. 25 値と欠け")
        curve, src = hn.curve(1, date(2026, 10, 5))
        check("25 値（0 時 = 前日 24 時、24 時 = index 23）",
              curve is not None and len(curve) == 25 and curve[0] == 90 and curve[24] == 330 and src == 1)
        check("値の無い日は None", hn.curve(2, date(2026, 11, 5)) == (None, None))
        check("位置の無い地点は None", hn.at(9, datetime(2026, 10, 5, 13, 0)) == (None, None))
        check("ディレクトリが無ければ偽", not HourlyNormals(Path(tmp, "none"), lambda *a: None, {}))

    print("✓ ok" if ok else "✗ 時別の平年値の読み出しが壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
