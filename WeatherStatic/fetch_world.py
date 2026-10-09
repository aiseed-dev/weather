#!/usr/bin/env python3
"""世界の天気の取得ドライバ。worldtime-web(time-j.net)向けの /data/world/ を生成する。

出力(毎回上書き。増えない):
    data/world/forecast/{place}.json … met.no 予報(112都市、hourly 48h + daily 8日)
    data/world/metar/{icao}.json     … METAR 実測(385局。通報の無い局は前回値を残す)
    data/world/index.json            … 提供一覧と更新時刻
    → 最後に public/data/world/ へ同期する(CORS 等のヘッダは generate.py が
      public/_headers に書く。/data/world/* の項)

都市マスター: master/world_cities.json(worldtime-web 側で生成)
失敗時は例外で停止し、既存の data/world/* は変更しない(前回値で配信継続)。

usage:
    .venv/bin/python fetch_world.py                 # 予報 + METAR + 同期
    .venv/bin/python fetch_world.py --metar-only    # METAR だけ(高頻度 cron 用)
    .venv/bin/python fetch_world.py --limit 5       # 動作確認用
    .venv/bin/python fetch_world.py --sync-only     # public/ への同期だけ
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from weatherlib import world

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
WORLD = BASE / "data" / "world"
PUBLIC_WORLD = BASE / "public" / "data" / "world"


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


def fetch_forecasts(cities: list[dict], fetched: str, limit: int) -> list[str]:
    targets = [c for c in cities if c["forecast"]]
    if limit:
        targets = targets[:limit]
    places = []
    for n, city in enumerate(targets, 1):
        raw = world.fetch_metno(city["lat"], city["lng"])
        write_json(WORLD / "forecast" / f'{city["place"]}.json',
                   world.transform_forecast(raw, city, fetched))
        places.append(city["place"])
        if n % 20 == 0:
            print(f"  予報 {n}/{len(targets)}")
        time.sleep(world.REQUEST_INTERVAL)
    print(f"  予報 {len(places)} 都市")
    return places


def fetch_metars(cities: list[dict], fetched: str, limit: int) -> list[str]:
    icaos = sorted({c["icao"] for c in cities if c["icao"]})
    if limit:
        icaos = icaos[:limit]
    obs = world.fetch_metars(icaos)
    for icao, o in obs.items():
        o["fetched"] = fetched
        write_json(WORLD / "metar" / f"{icao}.json", o)
    missing = len(icaos) - len(obs)
    print(f"  METAR {len(obs)}/{len(icaos)} 局(通報なし {missing}。前回値を維持)")
    return icaos


def sync_public() -> None:
    if not WORLD.exists():
        raise SystemExit("data/world/ がありません。先に取得を実行してください")
    if PUBLIC_WORLD.exists():
        shutil.rmtree(PUBLIC_WORLD)
    shutil.copytree(WORLD, PUBLIC_WORLD)
    n = sum(1 for p in PUBLIC_WORLD.rglob("*") if p.is_file())
    print(f"  public/data/world/ へ同期({n} ファイル)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast-only", action="store_true")
    ap.add_argument("--metar-only", action="store_true")
    ap.add_argument("--sync-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="対象件数を制限(動作確認用)")
    ap.add_argument("--no-sync", action="store_true", help="public/ へ同期しない")
    args = ap.parse_args()

    if args.sync_only:
        sync_public()
        return

    with open(MASTER / "world_cities.json", encoding="utf-8") as f:
        cities = json.load(f)["cities"]
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds") \
        .replace("+00:00", "Z")

    index_path = WORLD / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) \
        if index_path.exists() else {}

    if not args.metar_only:
        index["forecast"] = fetch_forecasts(cities, fetched, args.limit)
        index["forecast_updated"] = fetched
    if not args.forecast_only:
        index["metar"] = fetch_metars(cities, fetched, args.limit)
        index["metar_updated"] = fetched
    write_json(index_path, index)

    if not args.no_sync:
        sync_public()


if __name__ == "__main__":
    main()
