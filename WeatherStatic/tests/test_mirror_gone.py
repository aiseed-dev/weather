#!/usr/bin/env python3
"""10 分値ミラーが、気象庁で 404 が確定したスロットを取りに行き続けないこと。

移行で止まっていた期間の欠けは、保持期間（約 9 日）を過ぎると二度と取れない。
以前は「新しい順に 60 件」の枠をそれらが毎回占め、1 回 118 リクエスト
（やり直し込み）の 404 を気象庁に返させていた（2026-10-05）。

確かめること
  1. 出てから 1 時間以上たったスロットの 404 は記録し、次の実行では取りに行かない
  2. 出たばかりのスロットの 404 は記録しない（遅れているだけかもしれない）
  3. 確定した 404 はやり直さない（settled=True で呼ぶ）
  4. 取得窓より古い記録は捨てる
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))
import fetch_amedas_mirror as m  # noqa: E402

LATEST = datetime(2026, 10, 5, 14, 40)


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'OK' if cond else 'NG'} {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        m.OUT, m.MAP, m.GONE = root, root / "map", root / "data" / "gone.json"
        m.MAP.mkdir()
        m.period_path = lambda p: root / "archive-none"      # 封入済みの期間は無いことにする
        m.MAX_FETCH_PER_RUN = 1000

        # 直近 30 分（3 スロット）以外は手元にあることにし、9 日より前は気象庁にもう無い
        for ts in m.wanted_slots(LATEST):
            if LATEST - ts > timedelta(minutes=30) and LATEST - ts <= timedelta(days=9):
                (m.MAP / f"{m.slot_name(ts)}.json").write_text("{}")
        calls = []

        def fake(name, settled=False):
            ts = datetime.strptime(name, "%Y%m%d%H%M%S")
            calls.append((name, settled))
            if LATEST - ts > timedelta(days=9) or ts == LATEST:   # 古すぎる / 最新はまだ出ていない
                return None, "HTTP Error 404: Not Found"
            return b"{}", "JMA"
        m.fetch_slot = fake

        print("1 回目")
        m.fetch_missing(LATEST, dry=False)
        old = [c for c in calls if LATEST - datetime.strptime(c[0], "%Y%m%d%H%M%S") > timedelta(days=9)]
        gone = set(json.loads(m.GONE.read_text()))
        check("9 日より前の欠けを取りに行った", len(old) > 0)
        check("それらは settled（やり直さない）で呼んだ", all(s for _, s in old))
        check("404 が確定したものとして記録した", len(gone) == len(old))
        check("最新（出たばかり）の 404 は記録しない", m.slot_name(LATEST) not in gone)

        print("2 回目")
        calls.clear()
        m.fetch_missing(LATEST, dry=False)
        check("記録した古いスロットは取りに行かない",
              not any(n in gone for n, _ in calls))
        check("最新スロットは次の回も試す", any(n == m.slot_name(LATEST) for n, _ in calls))

        print("3. 取得窓より古い記録を捨てる")
        later = LATEST + timedelta(days=2)
        kept = m.load_gone(later)
        oldest = m.slot_name(later - timedelta(days=m.WINDOW_DAYS))
        check("窓の外の記録が消える", all(n > oldest for n in kept) and len(kept) < len(gone))

    print("✓ ok" if ok else "✗ 404 の記録が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
