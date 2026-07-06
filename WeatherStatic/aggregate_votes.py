#!/usr/bin/env python3
"""服装投票の集計。Web サーバーのアクセスログから vote.gif への GET を拾う。

静的サイトにバックエンドを持たないための方式:
  ブラウザ → GET /vote.gif?d=2026-07-06&c=47662&v=1   （1x1 GIF。generate.py が配置）
  → 配信サーバーのアクセスログに記録される
  → 本スクリプトがログを解析し store/weather.sqlite の votes_raw に蓄積
  → generate.py が「昨日の投票結果」を Home に表示

重複排除: (IP の SHA-1, 日付, 地点) の主キーで同一人の再投票は無視（INSERT OR IGNORE）。
何度同じログを流しても結果が変わらない（冪等）ので、ローテーション済みログもそのまま渡せる。

使い方:
    python aggregate_votes.py /var/log/nginx/access.log [access.log.1 ...]
    zcat access.log.*.gz | python aggregate_votes.py -
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

from weatherlib.store import open_store

BASE = Path(__file__).resolve().parent
SQLITE = BASE / "store" / "weather.sqlite"

# combined / common ログ形式: IP が行頭、リクエスト行が最初の引用符内
LINE = re.compile(r'^(\S+) \S+ \S+ \[[^\]]*\] "GET /vote\.gif\?([^ "]+)')


def parse_line(line: str):
    m = LINE.match(line)
    if not m:
        return None
    ip, qs = m.groups()
    q = parse_qs(qs)
    try:
        d = q["d"][0]
        code = int(q["c"][0])
        v = int(q["v"][0])
    except (KeyError, ValueError, IndexError):
        return None
    if v not in (0, 1) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return None
    return hashlib.sha1(ip.encode()).hexdigest(), d, code, v


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    conn = open_store(SQLITE)
    n_seen = n_new = 0
    for src in args:
        f = sys.stdin if src == "-" else open(src, encoding="utf-8", errors="replace")
        with f:
            for line in f:
                rec = parse_line(line)
                if rec is None:
                    continue
                n_seen += 1
                cur = conn.execute(
                    "INSERT OR IGNORE INTO votes_raw (ip_hash, date, code, v) "
                    "VALUES (?, ?, ?, ?)", rec)
                n_new += cur.rowcount
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM votes_raw").fetchone()[0]
    print(f"[votes] 投票ログ {n_seen} 件を処理、新規 {n_new} 票（累計 {total} 票）")
    conn.close()
    return 0




# ---- Cloudflare KV モード（Workers 版ビーコン。workers/vote/worker.js 参照）----
# 使い方: CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / VOTES_KV_NAMESPACE_ID を
# 環境変数か ~/.config/cloudflare/pages.env に置き、`aggregate_votes.py --kv`。
# キー v:{date}:{code}:{iphash} を votes_raw に INSERT OR IGNORE で取り込む。

def main_kv() -> int:
    import json
    import os
    import urllib.request

    envf = Path.home() / ".config" / "cloudflare" / "pages.env"
    if envf.is_file():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    ns = os.environ.get("VOTES_KV_NAMESPACE_ID")
    if not (token and acct and ns):
        print("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / VOTES_KV_NAMESPACE_ID が必要")
        return 1
    base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/storage/kv/namespaces/{ns}"

    def api(path):
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode()

    conn = open_store(SQLITE)
    n_new = 0
    cursor = ""
    while True:
        page = json.loads(api(f"/keys?prefix=v%3A&limit=1000&cursor={cursor}"))
        for k in page["result"]:
            _, d, code, iph = k["name"].split(":", 3)
            v = api(f"/values/{k['name']}").strip()
            cur = conn.execute(
                "INSERT OR IGNORE INTO votes_raw (ip_hash, date, code, v) VALUES (?,?,?,?)",
                (iph, d, int(code), int(v)))
            n_new += cur.rowcount
        cursor = page.get("result_info", {}).get("cursor") or ""
        if not cursor:
            break
    conn.commit()
    print(f"KV 取り込み: 新規 {n_new} 票")
    conn.close()
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main_kv() if "--kv" in _s.argv else main())
