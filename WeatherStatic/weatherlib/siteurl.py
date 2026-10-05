"""サイトの URL は小文字にそろえる（正規化）。

なぜ小文字か
    Cloudflare Pages はパスの大文字小文字を区別し、_redirects の一致も区別する。
    旧サイト（ASP.NET）は区別しなかったので、旧サイトへのリンクは /Stations/JP/Tokyo・
    /stations/jp/tokyo のように表記が揺れている。サイトの URL をすべて小文字にして、
    大文字を含む要求は小文字へ送れば（Cloudflare の転送ルール、無い環境では 404.html の
    スクリプト）、どの表記でも 1 つのページに着く。

生成側は、書き出すパスとページ内のサイト内リンクをここで小文字にする。テンプレートや
コードには旧サイトと同じ表記（/Summer/Ranking など）が残っていてよい。
"""
from __future__ import annotations

import re

# href="/…"・src="/…"・action="/…" のパス部分（? と # の手前まで）
_LINK = re.compile(r"""(\b(?:href|src|action)\s*=\s*["'])(/[^"'?#\s]*)""")


def path(rel: str) -> str:
    """書き出すパス（public/ からの相対）。"""
    return rel.lower()


def url(u: str) -> str:
    """サイト内の URL（パス部分だけ小文字。クエリと # はそのまま）。"""
    m = re.match(r"([^?#]*)(.*)", u, re.S)
    return m.group(1).lower() + m.group(2)


def links(html: str) -> str:
    """ページ内のサイト内リンクを小文字にする。外部リンク（https://…）は触らない。"""
    return _LINK.sub(lambda m: m.group(1) + m.group(2).lower(), html)
