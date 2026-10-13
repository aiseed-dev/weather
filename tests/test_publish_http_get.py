# SPDX-License-Identifier: AGPL-3.0-or-later
"""tools/publish_forecast.http_get: 途中で切れた・止まった取得を、受け取った所から続ける。"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import publish_forecast  # noqa: E402

BODY = bytes(range(256)) * 4096          # 1 MiB


def serve(behaviour):
    """behaviour(handler, start) が返す (送るバイト列, 送ったあと切るか, 止まる秒数)。"""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].rstrip("-")) if rng else 0
            requests.append(start)
            chunk, cut, stall = behaviour(len(requests), start)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(BODY) - start))
            self.end_headers()
            if chunk is not None and not isinstance(chunk, bytes):     # 細々と送る
                pos = start
                try:
                    while pos < len(BODY):
                        self.wfile.write(BODY[pos:pos + 1024])
                        self.wfile.flush()
                        pos += 1024
                        threading.Event().wait(0.02)
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.wfile.write(chunk)
                self.wfile.flush()
            if stall:
                threading.Event().wait(stall)
            if cut:
                self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/x.grib2", requests


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(publish_forecast, "READ_TIMEOUT", 0.5)
    monkeypatch.setattr(publish_forecast.time, "sleep", lambda s: None)


def test_resumes_after_cut(tmp_path):
    # 1 回目は 300KB で切れる。2 回目は残りを全部
    srv, url, reqs = serve(lambda n, start: (BODY[start:start + 300_000], True, 0) if n == 1
                           else (BODY[start:], False, 0))
    try:
        target = tmp_path / "a.grib2"
        publish_forecast.http_get(url, target)
        assert target.read_bytes() == BODY
        assert reqs == [0, 300_000]
        assert not (tmp_path / "a.grib2.part").exists()
    finally:
        srv.shutdown()


def test_resumes_after_stall(tmp_path):
    # 1 回目は 200KB 送って止まる（READ_TIMEOUT より長く）。続きから取り直す
    srv, url, reqs = serve(lambda n, start: (BODY[start:start + 200_000], True, 2.0) if n == 1
                           else (BODY[start:], False, 0))
    try:
        target = tmp_path / "b.grib2"
        publish_forecast.http_get(url, target)
        assert target.read_bytes() == BODY
        assert reqs[0] == 0 and reqs[1] > 0
    finally:
        srv.shutdown()


def test_reconnects_when_too_slow(tmp_path, monkeypatch):
    # 1 回目は 1KB ずつ細々と送る（止まりはしない）。遅すぎると見て繋ぎ直し、続きを取る
    monkeypatch.setattr(publish_forecast, "SLOW_WINDOW", 0.3)
    monkeypatch.setattr(publish_forecast, "SLOW_BYTES_PER_S", 100_000)

    class Trickle:
        pass

    def behaviour(n, start):
        if n == 1:
            return Trickle, True, 0
        return BODY[start:], False, 0

    srv, url, reqs = serve(behaviour)
    try:
        target = tmp_path / "d.grib2"
        publish_forecast.http_get(url, target)
        assert target.read_bytes() == BODY
        assert len(reqs) == 2 and reqs[1] > 0
    finally:
        srv.shutdown()


def test_gives_up_and_leaves_nothing(tmp_path):
    srv, url, _ = serve(lambda n, start: (BODY[start:start + 1000], True, 0))
    try:
        target = tmp_path / "c.grib2"
        with pytest.raises(OSError):
            publish_forecast.http_get(url, target, retries=2)
        assert not target.exists()
        assert not (tmp_path / "c.grib2.part").exists()
    finally:
        srv.shutdown()
