# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Wall-clock benchmark: ECMWF Open Data bulk vs. selective download.

Compares two retrieval strategies against the three public mirrors
the catalog advertises (AWS, ECMWF direct, GCP):

  * **bulk**        — single HTTPS GET of the entire ``oper-fc.grib2``
                      file for one (run, step). Whatever the file
                      happens to contain, you get all of it.
  * **selective N** — ``ecmwf-opendata`` ``Client.retrieve`` asking
                      for exactly ``N`` surface params. The client
                      reads the ``.index`` sidecar and issues HTTP
                      Range requests for just the GRIB messages that
                      cover those params.

Why this test exists
--------------------
Empirically the selective path is *slower per byte* (extra index
fetch + many small Range requests + multiurl coordination) but
*faster end-to-end* once N ≪ total messages. The crossover depends
on the mirror's TTFB and how aggressively it serves Range responses.
This benchmark gives a real number per (mirror, N) so we can decide
the download_concurrency tuning and tell users which mirror is best
for their network.

Running it
----------
Skipped by default — it hits the live network and downloads
hundreds of MB. To run::

    AISEED_BENCH=1 pytest tests/test_download_bench.py -s

Optional knobs::

    AISEED_BENCH_SOURCES=aws,ecmwf,google   # which mirrors to time
    AISEED_BENCH_STEP=12                    # forecast step in hours
    AISEED_BENCH_NS=10,20,30                # selective counts

Output is printed via ``capsys.disabled()`` so it shows up even
without ``-v``.
"""

from __future__ import annotations

import os
import shutil
import time
import urllib.request
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────
# Gate the whole module: real-network only, opt-in.
# ─────────────────────────────────────────────────────────────────────
pytestmark = pytest.mark.skipif(
    not os.environ.get("AISEED_BENCH"),
    reason="set AISEED_BENCH=1 to run the live-network download benchmark",
)


# 30 surface params spanning the common forecast use-cases. Each
# slice [:N] is itself a sensible "what an analyst might pick"
# subset, so the 10/20/30-param tiers stay meteorologically
# meaningful and not just a random alphabetical grab.
_PARAMS_30: tuple[str, ...] = (
    # 1-10: the classic charts
    "msl", "2t", "10u", "10v", "tp",
    "2d", "skt", "sp", "tcwv", "10fg",
    # 11-20: extended surface + wind
    "100u", "100v", "mn2t6", "mx2t6", "tprate",
    "ro", "sf", "asn", "rsn", "tcc",
    # 21-30: radiation, fluxes, waves, convection
    "ssr", "ssrd", "strd", "ttr", "ewss",
    "nsss", "swh", "mwp", "pp1d", "mucape",
)


# Per-mirror base URLs for the bulk HRES oper file. Path layout is
# common to every mirror: {date}/{HH}z/ifs/0p25/oper/{date}{HH}0000-{step}h-oper-fc.grib2
_BULK_BASE: dict[str, str] = {
    "aws":    "https://data.ecmwf.int/forecasts",  # fallback if S3 path unknown
    "ecmwf":  "https://data.ecmwf.int/forecasts",
    "google": "https://storage.googleapis.com/ecmwf-open-data",
}
# AWS exposes the bucket directly; prefer the eu-central-1 endpoint
# to match what ecmwf-opendata uses under the hood.
_BULK_BASE["aws"] = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"


def _bulk_url(source: str, run: datetime, step_hours: int) -> str:
    base = _BULK_BASE[source]
    return (
        f"{base}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/"
        f"{run:%Y%m%d}{run:%H}0000-{step_hours}h-oper-fc.grib2"
    )


def _http_download(url: str, target: Path) -> int:
    """Stream a URL to disk. Returns bytes written."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "aiseed-weather-bench/1.0"},
    )
    with urllib.request.urlopen(req) as r, target.open("wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
    return target.stat().st_size


def _selective_download(
    source: str,
    run: datetime,
    step_hours: int,
    params: Iterable[str],
    target: Path,
) -> int:
    """One ecmwf-opendata Client.retrieve call against the named mirror."""
    from ecmwf.opendata import Client
    client = Client(source=source, model="ifs", resol="0p25")
    client.retrieve(
        type="fc",
        step=step_hours,
        param=list(params),
        date=run.strftime("%Y-%m-%d"),
        time=run.hour,
        target=str(target),
    )
    return target.stat().st_size


def _latest_run(step_hours: int) -> datetime:
    """Discover the most recent published cycle that contains step_hours."""
    from ecmwf.opendata import Client
    # Any mirror works for the .latest probe; aws is the canonical fast one.
    run = Client(source="aws", model="ifs", resol="0p25").latest(
        type="fc", step=step_hours, param="msl",
    )
    if run.tzinfo is None:
        run = run.replace(tzinfo=timezone.utc)
    return run


def _env_list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


def _env_ints(name: str, default: str) -> list[int]:
    return [int(s) for s in _env_list(name, default)]


def _timed(fn, *args) -> tuple[float, int]:
    t0 = time.perf_counter()
    size = fn(*args)
    return time.perf_counter() - t0, size


# ─────────────────────────────────────────────────────────────────────
# The benchmark itself.
# ─────────────────────────────────────────────────────────────────────
def test_bulk_vs_selective(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """For each mirror, time one bulk download and one selective
    download per requested N. Prints a comparison table.

    No retries, no caching, no parallelism within a measurement — we
    want raw wall-clock for one fresh request.
    """
    sources = _env_list("AISEED_BENCH_SOURCES", "aws,ecmwf,google")
    step = int(os.environ.get("AISEED_BENCH_STEP", "12"))
    ns = _env_ints("AISEED_BENCH_NS", "10,20,30")
    assert max(ns) <= len(_PARAMS_30), (
        f"AISEED_BENCH_NS asks for {max(ns)} params; only "
        f"{len(_PARAMS_30)} are defined in _PARAMS_30"
    )

    run = _latest_run(step)
    rows: list[tuple[str, str, float, int]] = []
    errors: list[tuple[str, str, str]] = []

    for source in sources:
        # 1. Bulk: one big HTTPS GET of the whole oper-fc.grib2.
        target = tmp_path / f"bulk_{source}.grib2"
        try:
            elapsed, size = _timed(
                _http_download, _bulk_url(source, run, step), target,
            )
            rows.append(("bulk", source, elapsed, size))
        except Exception as exc:  # noqa: BLE001 — benchmark, report and continue
            errors.append(("bulk", source, repr(exc)))

        # 2. Selective N=10/20/30 via ecmwf-opendata Client.
        for n in ns:
            target = tmp_path / f"sel_{source}_{n}.grib2"
            try:
                elapsed, size = _timed(
                    _selective_download,
                    source, run, step, _PARAMS_30[:n], target,
                )
                rows.append((f"selective {n:2d}", source, elapsed, size))
            except Exception as exc:  # noqa: BLE001
                errors.append((f"selective {n}", source, repr(exc)))

    # ── Report ──
    with capsys.disabled():
        print()
        print(f"  run    = {run.isoformat()}")
        print(f"  step   = {step}h")
        print(f"  params = {_PARAMS_30}")
        print()
        header = f"  {'mode':<14} {'source':<8} {'time':>8}  {'bytes':>14}  {'MB/s':>6}"
        print(header)
        print("  " + "─" * (len(header) - 2))
        for mode, source, t, sz in rows:
            mbps = (sz / 1e6) / t if t > 0 else 0.0
            print(
                f"  {mode:<14} {source:<8} "
                f"{t:>7.2f}s  {sz:>14,}  {mbps:>6.1f}",
            )
        if errors:
            print()
            print("  ── errors ──")
            for mode, source, msg in errors:
                print(f"  {mode:<14} {source:<8} {msg}")
        print()

    # The benchmark is informational, but at least one measurement
    # must succeed — otherwise the network is dead and the numbers
    # above are meaningless.
    assert rows, f"every download failed: {errors}"
    for _mode, _source, _t, sz in rows:
        assert sz > 0
