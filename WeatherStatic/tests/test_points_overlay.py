#!/usr/bin/env python3
"""今日の最高・最低を地点別 10 分値で重ねる処理（fetch_data.overlay_points）。

確かめること
  1. 起時は UTC で来るので日本時間に直す（{"hour": 3, "minute": 21} → "12:21"）
  2. CSV より新しい同じ観測日のスロットなら重ね、表示時刻もそのスロットにする
  3. 今日の値が今年・当月・史上の記録を超えたら、その欄も今日にする
  4. 猛暑日などの地点数を、重ねた表から数え直す
  5. 重ねないとき: 品質フラグが 0 以外 / 観測日が違う（0 時の CSV は前日 24 時）
     / 毎正時 0 分のスロット / CSV より古いスロット
"""
import json
import sys
import tempfile
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
from weatherlib import pointstore  # noqa: E402
import fetch_data  # noqa: E402

ROW = {k: "" for k in fetch_data.TODAY_FIELDS}


def row(amedas, **kw):
    r = dict(ROW, code="1", amedas=amedas, tmax_q="4", tmin_q="4")
    r.update({k: str(v) for k, v in kw.items()})
    return r


def put_block(day: str, block: str, series: dict) -> None:
    d = pointstore.POINT / day / block
    d.mkdir(parents=True, exist_ok=True)
    (d / "test.json").write_text(json.dumps(series), encoding="utf-8")
    pointstore._load.cache_clear()


def entry(tmax, tmax_utc, tmin, tmin_utc, flag=0):
    return {"temp": [20.0, 0],
            "maxTemp": [tmax, flag], "maxTempTime": {"hour": tmax_utc[0], "minute": tmax_utc[1]},
            "minTemp": [tmin, 0], "minTempTime": {"hour": tmin_utc[0], "minute": tmin_utc[1]}}


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'OK' if cond else 'NG'} {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pointstore.POINT, pointstore.EXTRA, pointstore.MAP = root / "p", root / "e", root / "m"

        print("1〜4. 12:00 の CSV に 12:50 のスロットを重ねる")
        put_block("20261005", "12", {
            "44132": {"20261005125000": entry(36.2, (3, 21), 17.9, (1, 57))},   # 東京: 猛暑日に
            "11001": {"20261005125000": entry(15.0, (2, 0), 9.0, (20, 30), flag=1)},  # 品質 1
        })
        rows = [row("44132", tmax=196, tmax_at="12:00", tmin=179, tmin_at="10:57",
                    year_tmax=355, year_tmax_date="2026-08-01", month_tmax=300,
                    month_tmax_date="2020-10-01", record_tmax=398, record_tmax_date="2004-07-20"),
                row("11001", tmax=140, tmax_at="11:00", tmin=80, tmin_at="05:00")]
        meta = {"source_time": "2026-10-05T12:00", "counts": {}}
        out, m = fetch_data.overlay_points(rows, meta)
        t, s = out[0], out[1]
        check("表示時刻がスロットの 12:50 になる", m["source_time"] == "2026-10-05T12:50")
        check("最高 36.2 を重ね、起時を UTC 3:21 → 12:21 に直す",
              (t["tmax"], t["tmax_at"]) == ("362", "12:21"))
        check("最低の起時 UTC 1:57 → 10:57", t["tmin_at"] == "10:57")
        check("今年の記録 35.5 を超えたので今日にする",
              (t["year_tmax"], t["year_tmax_date"]) == ("362", "2026-10-05"))
        check("当月の記録も今日にする", t["month_tmax"] == "362")
        check("史上 39.8 は超えていないので触らない",
              (t["record_tmax"], t["record_tmax_date"]) == ("398", "2004-07-20"))
        check("品質フラグ 1 の最高は重ねない（CSV の 14.0 のまま）", s["tmax"] == "140")
        check("同じ地点でも品質 0 の最低は重ねる（UTC 20:30 → 05:30）",
              (s["tmin"], s["tmin_at"]) == ("90", "05:30"))
        check("猛暑日を数え直す（1 地点）", m["counts"]["moushobi"] == 1)
        check("重ねた地点数を記録する", m["points"]["stations"] == 2)
        check("元の表は書き換えない", rows[0]["tmax"] == "196")
        check("起時が null でも落ちない（空欄にする）",
              pointstore._jst_hhmm({"hour": None, "minute": None}) == "")

        print("5. 重ねない場合")
        out, m2 = fetch_data.overlay_points(rows, {"source_time": "2026-10-06T00:00", "counts": {}})
        put_block("20261006", "00", {"44132": {"20261006001000": entry(18.0, (15, 5), 17.0, (15, 5))}})
        out, m2 = fetch_data.overlay_points(rows, {"source_time": "2026-10-06T00:00", "counts": {}})
        check("0 時の CSV（前日 24 時）に翌日 0:10 のスロットは重ねない",
              m2["source_time"] == "2026-10-06T00:00" and "points" not in m2)
        put_block("20261006", "03", {"44132": {"20261006030000": entry(18.0, (15, 5), 17.0, (15, 5))}})
        out, m3 = fetch_data.overlay_points(rows, {"source_time": "2026-10-06T01:00", "counts": {}})
        check("01:00 の CSV に 00:10 は古いので重ねず、03:00（正時）は使う",
              m3["source_time"] == "2026-10-06T03:00")
        put_block("20261007", "00", {"44132": {"20261007000000": entry(18.0, (15, 5), 17.0, (15, 5))}})
        out, m4 = fetch_data.overlay_points(rows, {"source_time": "2026-10-06T23:00", "counts": {}})
        check("毎日 0 時 0 分のスロットは使わない", m4["source_time"] == "2026-10-06T23:00")

    print("✓ ok" if ok else "✗ 重ねる処理が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
