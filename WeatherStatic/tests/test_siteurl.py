#!/usr/bin/env python3
"""サイトの URL の小文字化（weatherlib/siteurl.py）。

確かめること
  1. ページ内のサイト内リンク（href・src・action）のパスを小文字にする
  2. クエリと # の後ろ、外部リンク、リンク以外の文字はそのまま
  3. 書き出すパスと URL の小文字化
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weatherlib import siteurl  # noqa: E402


def main() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'OK' if cond else 'NG'} {name}")
        ok &= bool(cond)

    html = ('<a href="/Stations/JP/Tokyo/">東京 Tokyo</a><img src="/Images/Chart/A.png">'
            '<a href="/Summer/Ranking?Year=2018#Top">x</a><a href="https://Example.com/Path">e</a>'
            "<form action='/Search/'></form><p>/Summer/Ranking</p>")
    out = siteurl.links(html)
    print("1. サイト内リンク")
    check("href のパスを小文字に", '<a href="/stations/jp/tokyo/">' in out)
    check("src も", 'src="/images/chart/a.png"' in out)
    check("action も（引用符が ' でも）", "action='/search/'" in out)
    print("2. そのままにするもの")
    check("クエリと # の後ろはそのまま", 'href="/summer/ranking?Year=2018#Top"' in out)
    check("外部リンクはそのまま", 'href="https://Example.com/Path"' in out)
    check("リンク以外の文字はそのまま", "東京 Tokyo" in out and "<p>/Summer/Ranking</p>" in out)
    print("3. パスと URL")
    check("書き出すパス", siteurl.path("Summer/Ranking/2018/index.html") == "summer/ranking/2018/index.html")
    check("URL はパスだけ小文字", siteurl.url("/Data/Daily/?Q=A#X") == "/data/daily/?Q=A#X")
    print("4. og:url・canonical の URL")
    check("トップ", siteurl.page_url("index.html") == "https://weather.time-j.net/")
    check("index.html は落として / で終える・小文字",
          siteurl.page_url("App/Forecast/index.html") == "https://weather.time-j.net/app/forecast/")
    check("index.html 以外はそのまま", siteurl.page_url("About/Foo.html") == "https://weather.time-j.net/about/foo.html")
    layout = ('<meta property="og:url" content="__PAGE_URL__">\n'
              '    <link rel="canonical" href="__PAGE_URL__">\n<title>x</title>')
    page = siteurl.with_page_url("Stations/JP/Tokyo/index.html", layout)
    check("目印を置き換える（og:url と canonical の両方）",
          page.count("https://weather.time-j.net/stations/jp/tokyo/") == 2 and siteurl.PAGE_URL_MARK not in page)
    gone = siteurl.with_page_url("404.html", layout)
    check("404.html は 2 行とも消す", gone == "<title>x</title>")

    print("✓ ok" if ok else "✗ URL の小文字化が壊れています")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
