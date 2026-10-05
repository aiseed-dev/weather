#!/usr/bin/env python3
"""pointstore の退避路（地点別が無い間は map ミラーを読む）を確かめる。

make_testdata.py は地点別の形でデータを作るので、退避路はテストデータでは
一度も通らない。2026-10-05、退避路がファイル名を 12 桁で組み立てていて
（実際の map ミラーは秒まで入った 14 桁）常に 0 地点を返していたのに、
deb2 で実データを生成するまで誰も気づかなかった。

map ミラーだけがある状態と、地点別もある状態の両方を作って見る。
"""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
from weatherlib import pointstore  # noqa: E402

DAY = date(2026, 10, 5)
SLOT = "202610051050"                  # 消費側が渡す 12 桁
MAP_ENTRY = {"temp": [15.5, 0], "precipitation10m": [0.0, 0]}


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pointstore.POINT = root / "point"
        pointstore.EXTRA = root / "extra"
        pointstore.MAP = root / "map"
        pointstore.MAP.mkdir()
        # ミラーと同じ 14 桁のファイル名
        (pointstore.MAP / f"{SLOT}00.json").write_text(
            json.dumps({"11001": MAP_ENTRY, "11016": MAP_ENTRY}), encoding="utf-8")

        print("1. map ミラーだけのとき")
        slots = pointstore.available_slots(DAY)
        view = pointstore.slot_view(SLOT)
        series = pointstore.station_series("11001", DAY)
        for name, good in (("available_slots が 12 桁で返す", slots == [SLOT]),
                           ("slot_view が 2 地点を返す", len(view) == 2),
                           ("slot_view の中身が map どおり", view.get("11001") == MAP_ENTRY),
                           ("station_series が 1 スロット返す", len(series) == 1)):
            print(f"  {'OK' if good else 'NG'} {name}")
            ok &= good

        print("2. 地点別もあるときは地点別が優先されること")
        area = pointstore.POINT / f"{DAY:%Y%m%d}" / "09"
        area.mkdir(parents=True)
        (area / "011000.json").write_text(json.dumps(
            {"11001": {f"{SLOT}00": {"temp": [16.0, 0], "prefNumber": 11}}}), encoding="utf-8")
        pointstore._load.cache_clear()
        view = pointstore.slot_view(SLOT)
        good = view == {"11001": {"temp": [16.0, 0]}}
        print(f"  {'OK' if good else 'NG'} 地点別の値を採り、観測要素でない prefNumber を除く")
        ok &= good

    print("✓ ok" if ok else "✗ 退避路が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
