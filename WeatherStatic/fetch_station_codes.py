#!/usr/bin/env python3
"""地点番号（code）の一覧を取得する。

ソース 1: etrn（過去の気象データ検索）の地点選択ページ
    https://www.data.jma.go.jp/stats/etrn/select/prefecture.php?prec_no=88&block_no=&...
    → 全府県の viewPoint(...) から 地点番号・名前・座標・観測要素・観測終了日
      （現役だけでなく**廃止地点も含む** → 過去データのバックフィル時に必要）

ソース 2（突合用）: 気象庁防災情報 XML のコード表
    https://xml.kishou.go.jp/tec_material.html の 個別コード表 zip 内
    20260326_PointAmedas.xlsx（アメダス地点マスタ。master/raw/jmaxml_code.zip に置くと突合する）

出力: master/station_codes.json
    build_master.py はこのファイルがあれば etrn の再取得をせずにこれを使う。

実行: python fetch_station_codes.py
"""
from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weatherlib import etrn

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
OUT = MASTER / "station_codes.json"
JMAXML_ZIP = MASTER / "raw" / "jmaxml_code.zip"


def log(msg: str) -> None:
    print(f"[codes] {msg}", flush=True)


def check(entries: list[dict]) -> None:
    """取得結果の整合チェック。"""
    act = [e for e in entries if e["active"]]
    log(f"取得: 全 {len(entries)} 地点（現役 {len(act)} / 廃止 {len(entries) - len(act)}）")
    for typ, label in (("s", "官署"), ("a", "アメダス")):
        n = sum(1 for e in act if e["type"] == typ)
        log(f"  現役 {label}: {n}")

    # 官署の地点番号は 5 桁（国際地点番号）、アメダスは 4 桁 — 例外を検出
    bad = [e for e in act
           if (e["type"] == "s") != (len(e["block_no"]) == 5)]
    if bad:
        log(f"  ✗ 桁数の例外: {[(e['block_no'], e['name']) for e in bad[:5]]}")

    # 主キー適格性: 現役地点の番号が全国一意か
    # （複数府県ページへのクロス掲載〈例: 富士山 = 山梨・静岡両方に掲載〉は重複ではない）
    by_block: dict[int, list] = {}
    for e in act:
        by_block.setdefault(int(e["block_no"]), []).append(e)
    real_dups = {k: v for k, v in by_block.items()
                 if len(v) > 1 and len({(e["name"], e["type"]) for e in v}) > 1}
    cross = {k: v for k, v in by_block.items()
             if len(v) > 1 and len({(e["name"], e["type"]) for e in v}) == 1}
    if real_dups:
        for k, v in list(real_dups.items())[:5]:
            log(f"  ✗ 現役地点番号の重複（別地点）: {k}: {[(e['prec_no'], e['name']) for e in v]}")
    else:
        log(f"  現役地点番号の全国一意性: ✓（ユニーク {len(by_block)} 地点）")
    for k, v in cross.items():
        log(f"  情報: {k} {v[0]['name']} は複数府県にクロス掲載 {[e['prec_no'] for e in v]}")

    # 廃止を含めた番号の再利用（過去データ利用時の注意）
    all_by_block: dict[int, set] = {}
    for e in entries:
        all_by_block.setdefault(int(e["block_no"]), set()).add((e["name"], e["type"]))
    n_reuse = sum(1 for v in all_by_block.values() if len(v) > 1)
    log(f"  廃止含む番号の再利用（別地点への転用）: {n_reuse} 番号")


def crosscheck_pointamedas(entries: list[dict]) -> None:
    """公式コード表（PointAmedas.xlsx）と座標で突合する。"""
    if not JMAXML_ZIP.exists():
        log(f"突合スキップ: {JMAXML_ZIP} が無い（xml.kishou.go.jp の個別コード表 zip を置くと突合）")
        return
    try:
        import openpyxl
    except ImportError:
        log("突合スキップ: openpyxl が未インストール")
        return

    z = zipfile.ZipFile(JMAXML_ZIP)
    name = next((n for n in z.namelist() if re.search(r"PointAmedas\.xlsx$", n)), None)
    if name is None:
        log("突合スキップ: zip 内に PointAmedas.xlsx が見つからない")
        return
    wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True)
    ws = wb["ame_master"]

    official = {}   # (緯度度,分, 経度度,分) → (観測所番号, 名前)
    for r in ws.iter_rows(min_row=3, values_only=True):
        # 列: 振興局, 観測所番号, 種類, 観測所名, ひらがな, 所在地, 緯度度, 緯度分, 経度度, 経度分, ...
        if r[1] is None or not str(r[1]).strip().isdigit():
            continue
        try:
            key = (int(r[6]), round(float(r[7]), 1), int(r[8]), round(float(r[9]), 1))
        except (TypeError, ValueError):
            continue
        official[key] = (str(r[1]), str(r[3]))
    log(f"公式コード表（PointAmedas）: {len(official)} 地点")

    act = [e for e in entries if e["active"]]
    matched = unmatched = 0
    samples = []
    for e in act:
        key = (e["lat_dm"][0], round(e["lat_dm"][1], 1),
               e["lon_dm"][0], round(e["lon_dm"][1], 1))
        if key in official:
            matched += 1
        else:
            unmatched += 1
            samples.append((e["block_no"], e["name"]))
    log(f"  etrn 現役 {len(act)} 地点のうち公式表と座標一致: {matched}"
        + (f" / 不一致 {unmatched} 例: {samples[:6]}" if unmatched else " （全一致 ✓）"))


def main() -> int:
    entries = etrn.fetch_all(log=log)
    check(entries)
    crosscheck_pointamedas(entries)

    obj = {
        "_meta": {
            "fetched_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
            "source": etrn.URL_PREF.replace("{prec}", "NN"),
            "entry_count": len(entries),
        },
        "entries": entries,
    }
    MASTER.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT)
    log(f"{OUT.relative_to(BASE)} を出力（{len(entries)} 地点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
