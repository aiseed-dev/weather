#!/usr/bin/env python3
"""観測所情報を取得し、前回との差分を示す。**履歴は git が持つ**。

なぜ adoc か
------------
officework の SEKKEI に「数のデータは .adoc の表、大きければ parquet」とある。
観測所は 1,286 行・8 項目・約 89KB なので **adoc の表**の範囲。JSON より
git の差分が読みやすい（1 地点 1 行で、項目が桁で揃う）。

**原本は adoc**。JSON は adoc から起こす派生物にする。人が読んで直せる形を
正に置くのが officework の考え方で、観測所情報のように「気象庁の変更を
人が読んで判断する」ものには合っている。

なぜ git か
-----------
観測所は増減し、番号も要素も変わる。その履歴こそが価値なので、履歴を
持つ仕組みに置くのが素直だった。当初 SQLite に station_change という表を
作って差分を自前で記録していたが、**git が最初からやっていることの
作り直し**でしかない。

    git log -p stations/amedastable.json    # いつ何が変わったか
    git diff stations/                       # 今回の変更

なぜ見張るか
------------
気づかないと**黙って欠測を作る**:

  新設地点を取りに行かない  → その地点のデータが永久に欠ける
  廃止地点を取り続ける      → 404 が並び、本当の障害が埋もれる
  番号が変わった            → 変更前後で別地点として分断される
  観測要素が増えた          → 新しい要素を取りこぼす

10 分値は 10 日で消えるので、**気づくのが遅れると取り返せない**。

使い方:
    python watch_stations.py          # 取得して差分を表示（ファイルは書き換える）
    python watch_stations.py --check  # 書き換えず、差分の有無だけ見る

差分があれば終了コード 1。cron から「変わったときだけ知らせる」に使える。
"""
from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

from weatherlib import jma
from weatherlib.station_adoc import dump_adoc, load_adoc

BASE = Path(__file__).resolve().parent
STATIONS = BASE / "stations"          # git で履歴を持つ原本
URL_TABLE = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
URL_MASTER = "https://www.jma.go.jp/jma/kishou/know/amedas/ame_master.zip"

# 差分として報告する項目。ここに無い項目が変わっても黙る（座標の丸め差などで
# 毎回鳴ると、本当の変更が埋もれる）
WATCHED = ("kjName", "type", "elems", "lat", "lon", "alt")


def log(msg: str) -> None:
    print(f"[watch] {msg}", flush=True)


def fetch_table() -> dict:
    return json.loads(jma.http_get(URL_TABLE))


def fetch_master_csv() -> str:
    """ame_master を UTF-8 の CSV にして返す。CP932 のままだと git の
    差分が読めないので変換して置く。"""
    raw = jma.http_get(URL_MASTER)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        return z.read(name).decode("cp932")


def diff(prev: dict, cur: dict) -> tuple[list, list, list]:
    added = sorted(set(cur) - set(prev))
    removed = sorted(set(prev) - set(cur))
    changed = []
    for code in sorted(set(cur) & set(prev)):
        for f in WATCHED:
            a, b = prev[code].get(f), cur[code].get(f)
            if a != b:
                changed.append((code, f, a, b))
    return added, removed, changed


def main() -> int:
    check_only = "--check" in sys.argv
    STATIONS.mkdir(parents=True, exist_ok=True)
    table_path = STATIONS / "amedastable.json"

    cur = fetch_table()
    log(f"amedastable.json: {len(cur)} 地点")

    adoc_path = STATIONS / "amedastable.adoc"
    prev = load_adoc(adoc_path) if adoc_path.exists() else {}
    first = not prev

    added, removed, changed = diff(prev, cur)
    n = len(added) + len(removed) + len(changed)

    if first:
        log(f"初回。{len(cur)} 地点を stations/ に置く（差分は次回から）")
    else:
        for c in added:
            t = cur[c]
            log(f"  **新設** {c} {t.get('kjName')} type={t.get('type')} "
                f"elems={t.get('elems')}")
        for c in removed:
            log(f"  **廃止** {c} {prev[c].get('kjName')}")
        for c, f, a, b in changed:
            log(f"  **変更** {c} {cur[c].get('kjName')} {f}: {a} → {b}")
        log("変更なし" if not n else f"変更 {n} 件"
            f"（新設 {len(added)} / 廃止 {len(removed)} / 変更 {len(changed)}）")

    if check_only:
        return 1 if n and not first else 0

    # 原本は adoc（人が読んで直せる形）。JSON は使う側のための派生物
    adoc_path.write_text(dump_adoc(cur), encoding="utf-8")
    table_path.write_text(
        json.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    try:
        (STATIONS / "ame_master.csv").write_text(fetch_master_csv(), encoding="utf-8")
    except Exception as exc:
        log(f"ame_master を取得できず（{exc}）。amedastable のみ更新した")

    if n and not first:
        log("次にやること: git diff stations/ で変更を読む → "
            "python build_area_map.py → git add stations/ && git commit")
    return 1 if (n and not first) else 0


if __name__ == "__main__":
    sys.exit(main())
