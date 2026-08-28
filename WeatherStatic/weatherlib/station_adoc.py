"""観測所情報を AsciiDoc の表で読み書きする。

なぜ adoc か
------------
officework の SEKKEI に「数のデータは `.adoc` の表、大きければ `parquet`」と
ある。観測所は 1,286 行・8 項目・約 89KB なので表の範囲。

JSON より git の差分が読みやすい。JSON だと 1 地点が複数行に散り、
項目名が毎行くり返されるが、表なら **1 地点 1 行**で桁が揃う。気象庁の
変更を人が読んで判断する用途に合う。

原本はこの adoc。JSON は使う側のために起こす派生物。
"""
from __future__ import annotations

from pathlib import Path

# 表の列。amedastable.json の項目に対応する。lat/lon は [度, 分] の組
COLUMNS = ("code", "kjName", "knName", "enName", "type", "elems",
           "lat_d", "lat_m", "lon_d", "lon_m", "alt")

HEADER = """= アメダス観測所
:source: https://www.jma.go.jp/bosai/amedas/const/amedastable.json
:generated-by: watch_stations.py

気象庁の現況配信（map JSON）に出る観測所。**これが正**で、`map/{ts}.json` に
現れる地点と過不足なく一致する。

変更は git の履歴で追う:

  git log -p stations/amedastable.adoc
  git diff stations/

`elems` は観測要素の桁（1=気温 2=降水 3=風 4=日照 5=? 6=積雪 7=湿度 8=視程）。
`type` の D/E/F/G は例外地点（父島・南鳥島・富士山・秩父別）で、
平地と同じ順位表や配色に混ぜないこと。

[cols="1,2,2,2,1,1,>1,>1,>1,>1,>1",options="header"]
|===
|番号 |地点名 |カナ |英名 |種別 |要素 |緯度度 |緯度分 |経度度 |経度分 |標高
"""


def dump_adoc(table: dict) -> str:
    """amedastable の辞書を adoc の表にする。番号順で安定させる。"""
    out = [HEADER]
    for code in sorted(table):
        r = table[code]
        lat, lon = r.get("lat") or [0, 0], r.get("lon") or [0, 0]
        cells = [code, r.get("kjName", ""), r.get("knName", ""),
                 r.get("enName", ""), r.get("type", ""), r.get("elems", ""),
                 lat[0], lat[1], lon[0], lon[1], r.get("alt", "")]
        out.append("|" + " |".join(str(c) for c in cells))
    out.append("|===\n")
    return "\n".join(out)


def load_adoc(path: Path) -> dict:
    """adoc の表を amedastable と同じ形の辞書に戻す。

    表の中だけを読む（`|===` で挟まれた範囲。見出し行は列名なので飛ばす）。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("|===")
    except ValueError:
        return {}
    out: dict = {}
    in_table = False
    for line in lines[start + 1:]:
        if line.startswith("|==="):
            break
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line[1:].split(" |")]
        if not in_table:            # 最初の | 行は見出し
            in_table = True
            continue
        if len(cells) != len(COLUMNS):
            continue
        code = cells[0]

        def num(s, cast=float):
            try:
                return cast(s)
            except ValueError:
                return 0

        out[code] = {
            "kjName": cells[1], "knName": cells[2], "enName": cells[3],
            "type": cells[4], "elems": cells[5],
            "lat": [num(cells[6], int), num(cells[7])],
            "lon": [num(cells[8], int), num(cells[9])],
            "alt": num(cells[10], int),
        }
    return out
