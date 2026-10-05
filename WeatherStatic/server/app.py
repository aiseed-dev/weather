"""個人開発気象統計を deb2 から配る FastAPI アプリ（Cloudflare Pages へ移るまでのつなぎ）。

サイトは静的ファイル（public/）だけでできていて、本番は Cloudflare Pages に置く。
このサーバーは public/ を Pages と同じ規則で返すだけで、ページを作ることはしない。
Pages を使わずに deb2 から配るときや、手元で公開前の見た目を確かめるときに使う。

Pages と同じ規則（順に判定する）
    1. 大文字を含む URL は小文字へ 301（URL は小文字にそろえている。weatherlib/siteurl.py。
       本番は Cloudflare の転送ルールが同じことをする）
    2. 生成済みの _redirects に当たれば、その行のとおり。200 は URL を変えずに行き先の
       ファイルを返す（過去の記録のページの枠。weatherlib/history.py）。
       Pages と同じく、実ファイルより先に効く
    3. 生成済みのファイルがあれば返す（ディレクトリはスラッシュ付きへ 308）
    4. どれにも当たらなければ 404（public/404.html）

起動（deb2）:
    cd ~/dev/weather/WeatherStatic
    ../.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8770
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

BASE = Path(__file__).resolve().parent.parent
PUBLIC = (BASE / "public").resolve()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

MEDIA = {".nc": "application/x-netcdf", ".json": "application/json",
         ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


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
                code = int(parts[2]) if len(parts) > 2 else 302
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


def local(url: str) -> Path | None:
    """URL に当たる public/ のファイル（ディレクトリは index.html）。public/ の外は None。"""
    p = (PUBLIC / url.lstrip("/")).resolve()
    if PUBLIC not in p.parents and p != PUBLIC:
        return None
    if p.is_dir():
        p = p / "index.html"
    return p if p.is_file() else None


def file_response(p: Path) -> Response:
    rel = p.relative_to(PUBLIC).as_posix()
    headers = {}
    if p.suffix == ".html":
        headers["Cache-Control"] = "public, max-age=60"
    if rel.startswith("data/"):
        headers["Access-Control-Allow-Origin"] = "*"
        if p.suffix == ".nc":
            headers["Cache-Control"] = "public, max-age=86400"
    return FileResponse(p, media_type=MEDIA.get(p.suffix), headers=headers)


def not_found() -> Response:
    p = PUBLIC / "404.html"
    body = p.read_text(encoding="utf-8") if p.is_file() else "<h1>404</h1>"
    return HTMLResponse(body, status_code=404)


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def serve(path: str, request: Request) -> Response:
    url = "/" + path
    query = ("?" + request.url.query) if request.url.query else ""

    # 1. 大文字は小文字へ
    if url != url.lower():
        return RedirectResponse(url.lower() + query, status_code=301)

    # 2. _redirects（実ファイルより先）
    if hit := REDIRECTS.match(url):
        dst, code = hit
        if code == 200:
            p = local(dst)
            return file_response(p) if p else not_found()
        return RedirectResponse(dst + query, status_code=code)

    # 3. 生成済みのファイル
    p = (PUBLIC / path).resolve() if path else PUBLIC
    if PUBLIC in p.parents or p == PUBLIC:
        if p.is_file():
            return file_response(p)
        if p.is_dir() and (p / "index.html").is_file():
            if url.endswith("/"):
                return file_response(p / "index.html")
            return RedirectResponse(url + "/" + query, status_code=308)

    return not_found()
