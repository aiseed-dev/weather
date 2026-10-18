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


SITE_ORIGIN = "https://weather.time-j.net"
# テンプレート（_layout.html）の og:url・canonical に置く目印。write() が、そのページ自身の URL に置き換える
PAGE_URL_MARK = "__PAGE_URL__"
# 正規の URL を持たないページ（目印の行ごと消す）
NO_PAGE_URL = frozenset({"404.html"})


def page_url(rel: str) -> str:
    """書き出すパス（public/ からの相対）に対応する、公開の URL。

    index.html は落として / で終える。index.html 以外の html はそのまま。"""
    p = path(rel)
    if p == "index.html":
        p = ""
    elif p.endswith("/index.html"):
        p = p[: -len("index.html")]
    return f"{SITE_ORIGIN}/{p}"


_MARK_LINE = re.compile(r"^[ \t]*<[^>\n]*" + PAGE_URL_MARK + r"[^>\n]*>[ \t]*\n?", re.M)


def with_page_url(rel: str, html: str) -> str:
    """目印をこのページの URL に置き換える。NO_PAGE_URL のページは、目印の行を消す。"""
    if path(rel) in NO_PAGE_URL:
        return _MARK_LINE.sub("", html)
    return html.replace(PAGE_URL_MARK, page_url(rel))


def url(u: str) -> str:
    """サイト内の URL（パス部分だけ小文字。クエリと # はそのまま）。"""
    m = re.match(r"([^?#]*)(.*)", u, re.S)
    return m.group(1).lower() + m.group(2)


def links(html: str) -> str:
    """ページ内のサイト内リンクを小文字にする。外部リンク（https://…）は触らない。"""
    return _LINK.sub(lambda m: m.group(1) + m.group(2).lower(), html)
