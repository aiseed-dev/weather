#!/usr/bin/env python3
"""履歴蓄積ジョブ。気象庁の公開データを蓄積する（STORAGE_FORMATS.md 案 B）。

  観測値本体 → store/observations.nc（NetCDF-4 単一ファイル。station×time/date の配列）
  帳簿       → store/weather.sqlite（取込ログ・訂正ログ・地点マスタ）

cron で 1 日数回実行する。7 日窓で動くため、数日実行が止まっても欠測しない。

処理:
  1. アメダス map JSON（毎正時・全 1286 地点・過去 7 日分）→ temp/precip1h/sun1h
  2. mdrr 確定値 CSV（最高・最低、過去 7 日分）→ tmax/tmin（official・品質つき）
       毎回再取得し、値が変わっていたら訂正として correction_log に記録
  3. 日集計 → tavg（1〜24 時の毎正時 24 回平均）・precip・sun

クラッシュ耐性: observations.nc は「コピー → 更新 → rename」で原子的に置き換える。
初回実行時、旧スキーマ（SQLite に hourly/daily テーブルがある）なら NetCDF へ移行する。
"""
from __future__ import annotations

import shutil
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore", category=DeprecationWarning)  # netCDF4×numpy2.5 の内部警告

from weatherlib import jma
from weatherlib.ncstore import NcStore
from weatherlib.store import open_store

BASE = Path(__file__).resolve().parent
STORE_DIR = BASE / "store"
SQLITE = STORE_DIR / "weather.sqlite"
NC = STORE_DIR / "observations.nc"

JST = ZoneInfo("Asia/Tokyo")
WINDOW_DAYS = 7
MAP_INTERVAL = 0.3
CSV_INTERVAL = 0.3


def log(msg: str) -> None:
    print(f"[accumulate] {msg}", flush=True)


def now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


# ---------------------------------------------------------------- 1. map JSON → hourly

def ingest_map_hours(conn, ncs: NcStore) -> tuple[int, int]:
    now = now_jst()
    start = (now - timedelta(days=WINDOW_DAYS)).replace(minute=0, second=0, microsecond=0)
    end = (now - timedelta(minutes=20)).replace(minute=0, second=0, microsecond=0)

    done = {k for (k,) in conn.execute(
        "SELECT key FROM ingest_log WHERE kind IN ('map_hour', 'map_hour_404')")}

    n_ok = n_404 = 0
    seen: dict[str, str] = {}   # amedas → 最後に現れた正時（改番・廃止の検知用）
    ts = start
    while ts <= end:
        key = ts.strftime("%Y-%m-%dT%H:00")
        if key not in done:
            try:
                data = jma.fetch_amedas_map(ts)
                ncs.write_hour(ts, data)
                conn.execute("INSERT OR REPLACE INTO ingest_log VALUES ('map_hour', ?, ?)",
                             (key, now.isoformat()))
                for a in data:
                    seen[a] = key
                n_ok += 1
            except Exception as e:
                if "404" in str(e):
                    conn.execute(
                        "INSERT OR REPLACE INTO ingest_log VALUES ('map_hour_404', ?, ?)",
                        (key, now.isoformat()))
                    n_404 += 1
                else:
                    log(f"  警告: map {key} の取得に失敗: {e}")
            conn.commit()
            time.sleep(MAP_INTERVAL)
        ts += timedelta(hours=1)
    if seen:
        conn.executemany(
            "UPDATE stations SET last_seen = MAX(COALESCE(last_seen, ''), ?), "
            "first_seen = COALESCE(first_seen, ?) WHERE amedas = ?",
            [(k, k, a) for a, k in seen.items()])
        conn.commit()
    return n_ok, n_404


# ---------------------------------------------------------------- 2. 確定値 CSV → daily

def ingest_daily_csv(conn, ncs: NcStore) -> tuple[int, int]:
    # 確定値 CSV の更新は 1 日 4 回（5・13・19・翌 1 時頃）のみ。
    # 同じ更新スロット内での再取得は省略して気象庁への負荷を抑える。
    now = now_jst()
    slot_hour = max((h for h in (1, 5, 13, 19) if now.hour >= h), default=None)
    if slot_hour is None:   # 0 時台 → 前日の 19 時スロット
        slot = (now - timedelta(days=1)).strftime("%Y-%m-%d") + "T19"
    else:
        slot = now.strftime("%Y-%m-%d") + f"T{slot_hour:02d}"
    if conn.execute("SELECT 1 FROM ingest_log WHERE kind='daily_csv_sweep' AND key=?",
                    (slot,)).fetchone():
        log(f"確定値 CSV: スロット {slot} は取得済みのため省略")
        return 0, 0

    today = now_jst().date()
    n_days = n_corr = 0

    for i in range(1, WINDOW_DAYS + 1):
        d = today - timedelta(days=i)
        mmdd = d.strftime("%m%d")
        for url_tpl, field in ((jma.URL_MXTEM_DAY, "tmax"), (jma.URL_MNTEM_DAY, "tmin")):
            try:
                rows = jma.fetch_rct(url_tpl.format(mmdd=mmdd))
            except Exception as e:
                if "404" not in str(e):
                    log(f"  警告: {field} {d} の取得に失敗: {e}")
                time.sleep(CSV_INTERVAL)
                continue
            if not rows:
                log(f"  警告: {field} {mmdd} が空応答でした。スキップ")
                time.sleep(CSV_INTERVAL)
                continue
            content_date = (max(r.now for r in rows) - timedelta(hours=1)).date()
            if content_date != d:
                log(f"  警告: {field} {mmdd} の内容が {content_date} 分でした。スキップ")
                time.sleep(CSV_INTERVAL)
                continue

            # 地点マスタ（名前等）を更新。新地点は先に row を割り当ててから属性を書く。
            # 官署は code = 国際地点番号（アメダス単独点の code は build_master が etrn から解決）
            for r in rows:
                ncs.station_index(r.amedas)
            conn.executemany(
                "UPDATE stations SET intl = ?, pref = ?, name = ?, "
                "code = COALESCE(code, ?) WHERE amedas = ?",
                [(r.code or None, r.pref, r.name, r.code or None, r.amedas) for r in rows])

            data = {r.amedas: (r.temp, r.temp_time, r.quality)
                    for r in rows if r.temp != -999}
            corrections = ncs.update_daily_extreme(d, field, data)
            for amedas, old, new in corrections:
                conn.execute("INSERT INTO correction_log VALUES (?, ?, ?, ?, ?, ?)",
                             (now_jst().isoformat(), amedas, d.isoformat(), field, old, new))
            n_corr += len(corrections)
            conn.commit()
            time.sleep(CSV_INTERVAL)
        n_days += 1
    conn.execute("INSERT OR REPLACE INTO ingest_log VALUES ('daily_csv_sweep', ?, ?)",
                 (slot, now_jst().isoformat()))
    conn.commit()
    return n_days, n_corr


# ---------------------------------------------------------------- 3. 日集計

def aggregate_days(ncs: NcStore) -> int:
    today = now_jst().date()
    n = 0
    for i in range(1, WINDOW_DAYS + 1):
        n += ncs.aggregate_day(today - timedelta(days=i))
    return n


# ---------------------------------------------------------------- 旧スキーマからの移行

def migrate_from_sqlite(conn, ncs: NcStore) -> bool:
    """旧スキーマ（SQLite の hourly/daily テーブル）があれば NetCDF に移して削除する。"""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "hourly" not in tables:
        return False

    log("旧 SQLite スキーマを検出。observations.nc へ移行します...")
    # hourly: ts 単位で列に変換
    ts_list = [r[0] for r in conn.execute("SELECT DISTINCT ts FROM hourly ORDER BY ts")]
    for ts_s in ts_list:
        rows = conn.execute(
            "SELECT amedas, temp, precip1h, sun1h FROM hourly WHERE ts = ?", (ts_s,))
        data = {a: {"temp": t, "precip1h": p, "sun1h": s} for a, t, p, s in rows}
        ncs.write_hour(datetime.fromisoformat(ts_s), data)
    log(f"  hourly {len(ts_list)} 正時分を移行")

    # daily: 確定値（tmax/tmin）を移行（tavg 等は後段の集計で再計算される）
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM daily ORDER BY date")]
    for d_s in dates:
        d = date.fromisoformat(d_s)
        for field in ("tmax", "tmin"):
            rows = conn.execute(
                f"SELECT amedas, {field}, {field}_time, {field}_quality "
                f"FROM daily WHERE date = ? AND {field} IS NOT NULL", (d_s,))
            data = {a: (v, at or "", q or 0) for a, v, at, q in rows}
            if data:
                ncs.update_daily_extreme(d, field, data)
    log(f"  daily {len(dates)} 日分（tmax/tmin）を移行")

    conn.execute("DROP TABLE hourly")
    conn.execute("DROP TABLE daily")
    conn.commit()
    conn.execute("VACUUM")
    log("  旧テーブルを削除（VACUUM 済み）")
    return True


def main() -> int:
    started = time.monotonic()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    conn = open_store(SQLITE)

    # コピー → 更新 → rename（初回はワークファイルを直接新規作成）
    work = NC.with_suffix(".nc.work")
    if NC.exists():
        shutil.copy2(NC, work)
    elif work.exists():
        work.unlink()

    ncs = NcStore(work, conn)
    try:
        migrated = migrate_from_sqlite(conn, ncs)

        n_map, n_404 = ingest_map_hours(conn, ncs)
        log(f"map JSON: {n_map} ファイル取込" + (f"（{n_404} 件は提供期間外）" if n_404 else ""))

        n_days, n_corr = ingest_daily_csv(conn, ncs)
        log(f"確定値 CSV: {n_days} 日分を再取得" + (f"、訂正 {n_corr} 件を反映" if n_corr else "（訂正なし）"))

        n_agg = aggregate_days(ncs)
        log(f"日集計: {n_agg} 地点日を更新")
    finally:
        ncs.close()
        conn.commit()

    work.replace(NC)   # 原子的置き換え

    import os
    size = os.path.getsize(NC)
    n_st = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    log(f"observations.nc: {size/1e6:.1f} MB / {n_st} 地点 "
        f"({time.monotonic() - started:.1f} 秒)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
