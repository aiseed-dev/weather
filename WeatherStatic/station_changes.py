#!/usr/bin/env python3
"""観測所の新設・移転・廃止に、プログラムとして追随する。

なぜ要るか
----------
気象庁は観測所を新設し、移転し、廃止する。今のコードはどれも**黙って
進む**ので、気づかないまま欠測を作る:

  新設 … a2c.get() が None を返し、その地点を捨てる。実況にも地点ページ
         にも出ない。誰も気づかない
  廃止 … Worker が取りに行き 404。一時的な欠測と区別がつかず、
         本当の障害が 404 の山に埋もれる
  移転 … 座標が変わる。同じ番号のまま観測条件が変わるので、
         過去と接続してよいか判断が要る
  改番 … ncstore が「新地点」として別の row を割り当て、
         observations.nc の中で過去と切れる（1880 年からの連続性が壊れる）

最後が最も重い。10 分値は 10 日で消えるが、**統計の連続性は取り戻せない**。

何をするか
----------
stations/amedastable.adoc（git の原本）と手元の派生物を突き合わせ、
**追随に必要な作業を洗い出して指示する**。自動で直せるものは直し、
人の判断が要るものは止めて尋ねる。

判断が要るのは移転と改番:
  - 移転は「同じ地点として続けるか、別地点として切るか」が観測条件次第
  - 改番は「どの旧番号と繋ぐか」を機械が決められない

使い方:
    python station_changes.py            # 差分を調べて報告
    python station_changes.py --apply    # 自動で直せる分を直す
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weatherlib.station_adoc import load_adoc

BASE = Path(__file__).resolve().parent
STATIONS = BASE / "stations"
MASTER = BASE / "master"
SQLITE = BASE / "store" / "weather.sqlite"
JST = ZoneInfo("Asia/Tokyo")

# 座標がこれ以上動いたら「移転」とみなす。0.01 度 ≒ 1.1km。
# 測地系の見直しや小数の丸めで数メートル動くことがあるので、
# それを移転と呼ばない程度に鈍くしてある
MOVE_DEG = 0.01


def log(msg: str) -> None:
    print(f"[changes] {msg}", flush=True)


def latlon(rec: dict) -> tuple[float, float]:
    lat, lon = rec.get("lat") or [0, 0], rec.get("lon") or [0, 0]
    return lat[0] + lat[1] / 60, lon[0] + lon[1] / 60


def find_changes(cur: dict, prev: dict) -> dict:
    """配信の地点表（cur）と前回（prev）の差分を種類ごとに分ける。"""
    added = sorted(set(cur) - set(prev))
    removed = sorted(set(prev) - set(cur))
    moved, elems_changed, renamed = [], [], []
    for code in sorted(set(cur) & set(prev)):
        a, b = prev[code], cur[code]
        (la1, lo1), (la2, lo2) = latlon(a), latlon(b)
        d = ((la1 - la2) ** 2 + (lo1 - lo2) ** 2) ** 0.5
        if d > MOVE_DEG:
            moved.append((code, b.get("kjName"), (la1, lo1), (la2, lo2), d,
                          a.get("alt"), b.get("alt")))
        if a.get("elems") != b.get("elems"):
            elems_changed.append((code, b.get("kjName"), a.get("elems"), b.get("elems")))
        if a.get("kjName") != b.get("kjName"):
            renamed.append((code, a.get("kjName"), b.get("kjName")))

    # 改番の候補: 廃止された番号と新設された番号で、座標がほぼ同じもの。
    # **推測なので自動では繋がない**。人に示して判断を仰ぐ
    renumber = []
    for old in removed:
        o = latlon(prev[old])
        for new in added:
            n = latlon(cur[new])
            d = ((o[0] - n[0]) ** 2 + (o[1] - n[1]) ** 2) ** 0.5
            if d <= MOVE_DEG:
                renumber.append((old, prev[old].get("kjName"),
                                 new, cur[new].get("kjName"), d))
    return {"added": added, "removed": removed, "moved": moved,
            "elems": elems_changed, "renamed": renamed, "renumber": renumber}


def check_derived(cur: dict) -> dict:
    """派生物が配信に追いついているか。追いつくまで取りこぼす。"""
    out = {}
    am = MASTER / "area_map.json"
    if am.exists():
        area = json.loads(am.read_text(encoding="utf-8"))["stations"]
        out["area_map_missing"] = sorted(set(cur) - set(area))
        out["area_map_stale"] = sorted(set(area) - set(cur))
    ms = MASTER / "stations.json"
    if ms.exists():
        a2c = json.loads(ms.read_text(encoding="utf-8"))["index"]["amedas_to_code"]
        out["stations_missing"] = sorted(set(cur) - set(a2c))
        out["stations_stale"] = sorted(set(a2c) - set(cur))
    return out


def record_renumber(old: str, new: str) -> None:
    """改番を帳簿に残し、observations.nc の row を引き継ぐ。

    ncstore.station_index は未知の番号に新しい row を割り当てるので、
    **先にここで付け替えておく**。そうしないと 1880 年からの連続性が切れる。
    """
    conn = sqlite3.connect(SQLITE)
    now = datetime.now(JST).isoformat(timespec="seconds")
    row = conn.execute("SELECT row FROM stations WHERE amedas = ?", (old,)).fetchone()
    if row is None:
        log(f"  {old} は stations に無い（蓄積前の地点）。付け替えは不要")
        conn.close()
        return
    row = row[0]
    conn.execute("UPDATE stations SET amedas = ? WHERE row = ?", (new, row))
    conn.execute("UPDATE amedas_log SET valid_to = ? WHERE row = ? AND valid_to IS NULL",
                 (now, row))
    conn.execute("INSERT INTO amedas_log (row, amedas, valid_from, valid_to) "
                 "VALUES (?, ?, ?, NULL)", (row, new, now))
    conn.commit()
    conn.close()
    log(f"  row {row}: {old} → {new} に付け替え、有効期間を記録")


PREV = STATIONS / ".amedastable.prev.adoc"      # git が無い環境用の控え


def _previous(adoc: Path) -> "dict | None":
    """前回の版を返す。git があれば HEAD、無ければ控えのファイル。"""
    import shutil
    import subprocess
    if shutil.which("git"):
        r = subprocess.run(
            ["git", "show", "HEAD:WeatherStatic/stations/amedastable.adoc"],
            cwd=BASE.parent, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            tmp = Path("/tmp/_prev_stations.adoc")
            tmp.write_text(r.stdout, encoding="utf-8")
            return load_adoc(tmp)
    if PREV.exists():
        return load_adoc(PREV)
    return None


def _save_previous(adoc: Path) -> None:
    """次回の比較用に控える（git が無い環境で効く）。"""
    PREV.write_text(adoc.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> int:
    apply = "--apply" in sys.argv
    adoc = STATIONS / "amedastable.adoc"
    if not adoc.exists():
        log("stations/amedastable.adoc が無い。先に watch_stations.py を実行")
        return 2
    cur = load_adoc(adoc)

    # 前回の版。git があれば HEAD、無ければ控えのファイルを使う。
    # **git に依存させない** — 運用機（tgsvr）に git が入っていないことがあり、
    # そこで動かないと変更に気づけない（気づかないことが最大の失敗）。
    prev = _previous(adoc)
    if prev is None:
        log("前回の版が無い（初回）。控えを作る。差分は次回から")
        _save_previous(adoc)
        return 0

    ch = find_changes(cur, prev)
    n = sum(len(v) for v in ch.values())
    log(f"配信 {len(cur)} 地点 / git の版 {len(prev)} 地点")

    if ch["added"]:
        log(f"**新設 {len(ch['added'])}**")
        for c in ch["added"]:
            r = cur[c]
            log(f"  {c} {r.get('kjName')} type={r.get('type')} elems={r.get('elems')}")
    if ch["removed"]:
        log(f"**廃止 {len(ch['removed'])}**")
        for c in ch["removed"]:
            log(f"  {c} {prev[c].get('kjName')}")
    if ch["moved"]:
        log(f"**移転 {len(ch['moved'])}** — 過去と繋いでよいか判断が要る")
        for c, name, o, nw, d, a1, a2 in ch["moved"]:
            log(f"  {c} {name} {d*111:.1f}km 動いた 標高 {a1}→{a2}m")
    if ch["elems"]:
        log(f"**観測要素の変更 {len(ch['elems'])}** — 取得する要素が増減する")
        for c, name, a, b in ch["elems"]:
            log(f"  {c} {name} {a} → {b}")
    if ch["renamed"]:
        for c, a, b in ch["renamed"]:
            log(f"  改名 {c} {a} → {b}")
    if ch["renumber"]:
        log(f"**改番の候補 {len(ch['renumber'])}** — 座標が近い廃止/新設の組")
        for o, on, nn_, nname, d in ch["renumber"]:
            log(f"  {o} {on} → {nn_} {nname}（{d*111:.2f}km）")
        log("  自動では繋がない。正しければ --apply で row を引き継ぐ")

    der = check_derived(cur)
    stale = {k: v for k, v in der.items() if v}
    if stale:
        log("**派生物が追いついていない**")
        for k, v in stale.items():
            log(f"  {k}: {len(v)} 件 {v[:5]}")
        log("  直す: python build_area_map.py && python build_master.py")

    if not n and not stale:
        log("変更なし")
        return 0

    if apply:
        for o, _on, nn_, _nname, _d in ch["renumber"]:
            record_renumber(o, nn_)
        _save_previous(adoc)       # 反映したので、次回はここが基準
        log("自動で直せる分を反映した。build_area_map.py の実行も忘れずに")
    else:
        log("--apply で改番の付け替えを行う（座標が近いだけの別地点でないか確認してから）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
