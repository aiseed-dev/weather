#!/usr/bin/env python3
"""旧サイトの URL が、生成したサイトでどこに着くかを点検する（公開前の確認）。

legacy/old_paths.json の URL（旧サイトをリンクでたどって集めたもの）を、生成済みの
public/ と _redirects に対して Cloudflare Pages と同じ規則でたどる。

    1. 大文字は小文字へ（本番は Cloudflare の転送ルール）
    2. _redirects（実ファイルより先。200 は URL を変えずに行き先を返す）
    3. ファイル（ディレクトリは index.html）

結果は「そのまま」「転送して着く」「着かない」に分けて数え、着かないものを並べる。
着かないものが 1 件でもあれば終了コード 1。

使い方:
    python check_legacy_urls.py            # public/ を点検
    python check_legacy_urls.py --public DIR
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from server.app import Redirects  # noqa: E402


def resolve(public: Path, rules: Redirects, path: str) -> tuple[str, list[str]]:
    """(結果, たどった URL)。結果は ok / redirect / missing / loop。"""
    hops = []
    url = path
    for _ in range(6):
        if url != url.lower():
            hops.append(url)
            url = url.lower()
            continue
        hit = rules.match(url)
        if hit:
            dst, code = hit
            if code == 200:
                f = (public / dst.lstrip("/"))
                f = f / "index.html" if f.is_dir() else f
                return ("redirect" if hops else "ok") if f.is_file() else "missing", hops + [url]
            hops.append(url)
            url = dst
            continue
        f = public / url.lstrip("/")
        if f.is_dir() and (f / "index.html").is_file() or f.is_file():
            return ("redirect" if hops else "ok"), hops + [url]
        return "missing", hops + [url]
    return "loop", hops


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--public", default=str(BASE / "public"))
    args = ap.parse_args()
    public = Path(args.public).resolve()

    import server.app as srv
    srv.PUBLIC = public
    rules = Redirects()
    paths = json.loads((BASE / "legacy" / "old_paths.json").read_text(encoding="utf-8"))["paths"]
    counts, bad = Counter(), []
    for p in paths:
        result, hops = resolve(public, rules, p)
        counts[result] += 1
        if result in ("missing", "loop"):
            bad.append((p, result, hops))
    print(f"旧サイトの URL {len(paths)} 件: そのまま {counts['ok']}・転送して着く {counts['redirect']}・"
          f"着かない {counts['missing']}・回り続ける {counts['loop']}")
    for p, result, hops in bad[:40]:
        print(f"  {result:8s} {p}  ({' → '.join(hops)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
