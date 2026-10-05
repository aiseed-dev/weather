"""個人開発気象統計を deb2 から配る FastAPI アプリ。

なぜサーバーで配るか
    旧サイト（WeatherCore）の URL には、日ごと・月ごとの集計ページが 2,000 件以上
    あり、静的に作ると組み合わせの数だけページが要る。旧サイトは ASP.NET で
    大文字小文字を区別しなかったので、リンクの表記も揺れている。サーバーなら、
    集計はその場で作れ、大文字小文字も無視して引ける。しばらくは deb2 で運用する。

1 つの入口で受けて、次の順に判定する
    1. 生成済みのファイル（public/）がその URL にあれば返す。ディレクトリなら
       スラッシュ付きへ転送（Cloudflare Pages と同じ振る舞い）
    2. 過去のページ（旧 /Temperature/SummerDay/… など）ならその場で作る（history.py）
    3. 生成済みの _redirects に当たれば転送する（Pages と同じ規則で解釈する）
    4. 大文字小文字だけ違うファイルがあれば、正しい URL へ 301
    5. どれにも当たらなければ 404（public/404.html）

起動（deb2）:
    cd ~/dev/weather/WeatherStatic
    ../.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8770
"""
from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

BASE = Path(__file__).resolve().parent.parent
PUBLIC = (BASE / "public").resolve()
sys.path.insert(0, str(BASE))

from server import history  # noqa: E402

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

MEDIA = {".nc": "application/x-netcdf", ".json": "application/json",
         ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


# ---------------------------------------------------------------- テンプレート

_env = None


def env():
    """生成側（generate.py）と同じ Jinja 環境。見た目をサイトと揃える。"""
    global _env
    if _env is None:
        import generate
        _env = generate.make_env()
    return _env


# ---------------------------------------------------------------- 生成済みファイルの索引

class Index:
    """小文字にした URL → 実際のパス。大文字小文字の揺れを吸収するため。

    public/ は 10 分ごとに作り直されるが、ファイルの増減はまれなので、
    引けなかったときだけ（前回から INTERVAL 秒以上たっていれば）作り直す。"""
    INTERVAL = 60

    def __init__(self):
        self.map: dict[str, str] = {}
        self.built = 0.0

    def rebuild(self) -> None:
        m = {}
        for p in PUBLIC.rglob("*"):
            rel = "/" + p.relative_to(PUBLIC).as_posix()
            if p.is_dir():
                if (p / "index.html").is_file():
                    m[rel.lower() + "/"] = rel + "/"
            else:
                m[rel.lower()] = rel
        self.map, self.built = m, time.monotonic()

    def lookup(self, path: str) -> str | None:
        key = path.lower()
        if not key.endswith("/") and (key + "/") in self.map:
            key += "/"
        hit = self.map.get(key)
        if hit is None and time.monotonic() - self.built > self.INTERVAL:
            self.rebuild()
            hit = self.map.get(key) or self.map.get(key + "/")
        return hit


INDEX = Index()


# ---------------------------------------------------------------- _redirects

class Redirects:
    """生成済みの public/_redirects を Pages と同じ規則で解釈する。

    固定の行は完全一致、* を含む行は前方一致（* は空も含めて何にでも当たる）。
    書いてある順に見て、最初に当たった行を使う。ファイルが変われば読み直す。"""

    def __init__(self):
        self.mtime = -1.0
        self.static: dict[str, tuple[str, int]] = {}
        self.dynamic: list[tuple[str, str, int]] = []

    def load(self) -> None:
        p = PUBLIC / "_redirects"
        mt = p.stat().st_mtime if p.is_file() else 0.0
        if mt == self.mtime:
            return
        static, dynamic = {}, []
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) < 2 or parts[0].startswith("#"):
                    continue
                src, dst = parts[0], parts[1]
                code = int(parts[2]) if len(parts) > 2 else 301
                if "*" in src:
                    dynamic.append((src.split("*")[0], dst, code))
                else:
                    static.setdefault(src, (dst, code))
        self.static, self.dynamic, self.mtime = static, dynamic, mt

    def match(self, path: str) -> tuple[str, int] | None:
        self.load()
        if path in self.static:
            return self.static[path]
        for prefix, dst, code in self.dynamic:
            if path.startswith(prefix):
                return dst, code
        return None


REDIRECTS = Redirects()


# ---------------------------------------------------------------- 応答

def file_response(rel: str) -> Response:
    p = (PUBLIC / rel.lstrip("/")).resolve()
    if PUBLIC not in p.parents and p != PUBLIC:      # public/ の外は返さない
        return not_found()
    headers = {}
    if rel.endswith(".html"):
        headers["Cache-Control"] = "public, max-age=60"
    if rel.startswith("/Data/"):
        headers["Access-Control-Allow-Origin"] = "*"
        if rel.endswith(".nc"):
            headers["Cache-Control"] = "public, max-age=86400"
    return FileResponse(p, media_type=MEDIA.get(p.suffix), headers=headers)


def not_found() -> Response:
    p = PUBLIC / "404.html"
    body = p.read_text(encoding="utf-8") if p.is_file() else "<h1>404</h1>"
    return HTMLResponse(body, status_code=404)


def page(template: str, max_age: int, **ctx) -> Response:
    html = env().get_template(template).render(build_year=datetime.now().year, **ctx)
    return HTMLResponse(html, headers={"Cache-Control": f"public, max-age={max_age}"})


def past_max_age(d: date) -> int:
    """確定値に置き換わった過去の日は長く、直近は短くキャッシュさせる。"""
    return 86400 if (date.today() - d).days > 40 else 600


# ---------------------------------------------------------------- 過去のページ

DAY = re.compile(r"^/temperature/summerday/([abcd])(\d{8})/?$", re.I)
MONTH = re.compile(r"^/temperature/summermonth/([abcd])/(\d{1,2})/?$", re.I)


def day_page(kind: str, ymd: str) -> Response:
    try:
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    except ValueError:
        return not_found()
    r = history.day_ranking(kind, d)
    if r is None:
        return not_found()
    step = timedelta(days=1)
    return page("history/day.html", past_max_age(d),
                page_title=f"{d.year}年{d.month}月{d.day}日の{r['title']}の地点",
                nav_active="ranking", r=r,
                month_url=f"/Temperature/SummerMonth/{kind}/{d.month}",
                prev_url=f"/Temperature/SummerDay/{kind}{d - step:%Y%m%d}",
                next_url=f"/Temperature/SummerDay/{kind}{d + step:%Y%m%d}")


def month_page(kind: str, month: str) -> Response:
    m = int(month)
    if not 1 <= m <= 12:
        return not_found()
    t = history.month_table(kind, m)
    kinds = [(k, v[2].split("（")[0], f"/Temperature/SummerMonth/{k}/{m}")
             for k, v in history.KINDS.items()]
    months = [(mm, f"/Temperature/SummerMonth/{kind}/{mm}") for mm in range(5, 11)]
    return page("history/month.html", 600,
                page_title=f"{m}月の{t['title']}の日別の地点数", nav_active="ranking",
                t=t, kinds=kinds, months=months)


# ---------------------------------------------------------------- 入口

@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def serve(path: str, request: Request) -> Response:
    url = "/" + path

    # 1. 生成済みのファイル（そのままの綴り）
    p = (PUBLIC / path).resolve() if path else PUBLIC
    if PUBLIC in p.parents or p == PUBLIC:
        if p.is_file():
            return file_response(url)
        if p.is_dir() and (p / "index.html").is_file():
            if url.endswith("/"):
                return file_response(url + "index.html")
            return RedirectResponse(url + "/", status_code=308)

    # 2. 過去のページ（その場で作る。大文字小文字は問わない）
    if m := DAY.match(url):
        return day_page(m.group(1).lower(), m.group(2))
    if m := MONTH.match(url):
        return month_page(m.group(1).lower(), m.group(2))

    # 3. 旧サイトの URL の転送（生成済みの _redirects）
    if hit := REDIRECTS.match(url):
        return RedirectResponse(hit[0], status_code=hit[1])

    # 4. 大文字小文字だけ違う
    if (real := INDEX.lookup(url)) and real != url:
        return RedirectResponse(real, status_code=301)

    return not_found()
