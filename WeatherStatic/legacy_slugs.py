#!/usr/bin/env python3
"""旧サイト（weather.time-j.net の WeatherCore）の地点 URL 名を、地点番号に結び付けて保存する。

新しいサイトはいずれ同じドメインで旧サイトを置き換える。旧サイトの URL
（/Stations/JP/Abashiri・/Climate/Chart/Abashiri）で張られたリンクや検索結果が
切れないよう、新しいサイトも同じ URL 名で地点ページを作る（generate.py の
climate_targets が legacy/station_slugs.json を使う）。

旧サイトを止めたら二度と取れないので、旧サイトが動いているうちに取って
リポジトリに残す。旧サイトの一覧（/Stations/JP・/Climate）は
<h5 id="a{府県番号}">地方名</h5> の下に地点を並べているので、「府県番号 ＋
日本語の地点名」で新しい地点表（master/stations.json）と結び付ける。
ローマ字名は表記揺れ（Chyoshi・Erimomisaki）や同名の区別（Asahi-Toyama）が
あるので、推測で当てない。

使い方:
    python legacy_slugs.py [--stations master/stations.json]   # 下見（一致しなかったものを出す）
    python legacy_slugs.py --write                              # legacy/station_slugs.json を書く
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "legacy" / "station_slugs.json"
OLD = "https://weather.time-j.net"
UA = "WeatherStaticMigration/0.1 (URL compatibility, run by the site owner)"


def fetch(path: str) -> str:
    req = urllib.request.Request(OLD + path, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def parse_index(page: str, prefix: str) -> dict[str, tuple[int, str]]:
    """旧 URL 名 → (府県番号, 日本語名)。見出し h5 の id="a{府県番号}" の下を読む。"""
    out: dict[str, tuple[int, str]] = {}
    prec = None
    for m in re.finditer(r'<h5[^>]*id="a(\d+)"|<a[^>]+href="/' + re.escape(prefix)
                         + r'/([^"/]+)/?"[^>]*>(.*?)</a>', page, re.S):
        if m.group(1):
            prec = int(m.group(1))
        elif prec is not None:
            name = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip().rstrip("*")
            out.setdefault(m.group(2), (prec, name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="旧サイトの地点 URL 名を保存する")
    ap.add_argument("--stations", type=Path, default=BASE / "master" / "stations.json")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    old: dict[str, tuple[int, str]] = {}
    for page, prefix in (("/Stations/JP", "Stations/JP"), ("/Climate", "Climate/Chart")):
        got = parse_index(fetch(page), prefix)
        for slug, v in got.items():
            if slug in old and old[slug] != v:
                print(f"  注意: {slug} が一覧によって {old[slug]} と {v}")
            old.setdefault(slug, v)
        print(f"{page}: {len(got)} 地点")
        time.sleep(1)

    st = json.loads(args.stations.read_text(encoding="utf-8"))["stations"]
    if st and next(iter(st.values())).get("name") is None:
        print("地点表に名前がありません")
        return 1
    by_key: dict[tuple[int, str], list[str]] = {}
    for code, r in st.items():
        prec = (r.get("etrn") or {}).get("prec_no")
        if prec is not None and r.get("name"):
            by_key.setdefault((int(prec), r["name"]), []).append(code)

    mapping, unmatched, ambiguous = {}, [], []
    for slug, key in sorted(old.items()):
        codes = by_key.get(key, [])
        if not codes and "(" in key[1]:
            # 「南大東(南大東島)」「つくば(館野)」: 括弧の外か中の名前で引き直す
            outer, inner = key[1].split("(", 1)
            for alt in (outer.strip(), inner.rstrip(")").strip()):
                codes = by_key.get((key[0], alt), [])
                if codes:
                    break
        if len(codes) == 1:
            mapping[codes[0]] = slug
        elif not codes:
            unmatched.append((slug, key))
        else:
            ambiguous.append((slug, key, codes))
    print(f"結び付いた {len(mapping)} / 旧サイトの地点 {len(old)}")
    for slug, key in unmatched:
        print(f"  新しい地点表に無い: {slug} {key}")
    for slug, key, codes in ambiguous:
        print(f"  同じ府県・同じ名前が複数: {slug} {key} {codes}")

    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        # 別名（旧サイトを巡回して集めたもの）は一覧からは作れないので、書き直しても残す
        prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else {}
        OUT.write_text(json.dumps({
            "source": f"{OLD}/Stations/JP と {OLD}/Climate（旧 WeatherCore）",
            "note": "地点番号（code）→ 旧サイトの URL 名。/Stations/JP/{名}・/Climate/Chart/{名}",
            "slugs": dict(sorted(mapping.items(), key=lambda x: int(x[0]))),
            "unmatched": [{"slug": s, "prec_no": k[0], "name": k[1]} for s, k in unmatched],
            **{k: prev[k] for k in ("aliases", "aliases_note") if k in prev},
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{OUT.relative_to(BASE)} を書きました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
