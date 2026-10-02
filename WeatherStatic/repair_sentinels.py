#!/usr/bin/env python3
"""ダンプ由来の欠測センチネルを観測ストアから FILL に戻す。

backfill_daily.py の初回取込は欠測ガードが甘く（-999 だけを見ていた）、
ダンプの欠測センチネル（×10 整数の -9999 / -32768 / int32 最小値）や
品質注 0（欠測）の行が実数値として observations.nc に入り込んだ。
最小値・平均・ランキングが壊れるので、気候値を出す前に必ず修復する。

修復の定義:
    「修正後の取込ガード（v <= -999 / 品質 < 1 / 降水の負値 を捨てる）なら
    書かなかったはずのセル」をダンプと突き合わせて特定し、ストアの値が
    当時書かれた値（int16 折り返し後）のまま残っていれば FILL に戻す。
    値が変わっているセルは後続の蓄積・etrn 再取得が直したものなので触らない。

    閾値だけで消さないのは、int32 最小値が int16 に折り返すと 0（= 0.0℃）に
    化けて閾値では見えないため。逆に、ダンプに無いのに物理的にありえない値
    （気温 |v|≥60℃・降水の負値）が残っていれば「出所不明」として報告し、
    これも FILL に戻す（ありえない値を残す理由はない）。

    日本記録（最高 41.8℃ / 最低 -41.0℃）の外だが 60℃ 未満の値は報告だけ
    する。本物の異常値か転記ミスかは人が判断する。

読み書きは行単位で行う。libnetcdf 4.10.1（HDF5 2.2.0）には、変数自身の
書込み済み範囲より unlimited 次元が大きいとき、2 次元の広域読みが行の
詰め直しを誤り「ずれた配列＋未初期化メモリ」を返すバグがある（2026-10-02
に最小再現で確認）。行読み・書込み済み範囲内の読みは正しい。

使い方（ダンプは dev にしかないので dev で動かす）:
    python repair_sentinels.py ../weather.gz --store <storeのコピー>   # 下見
    python repair_sentinels.py ../weather.gz --store <storeのコピー> --apply

    本番ストアを直接指すのではなく、バックアップからのコピーに適用して
    検証し、observations.nc をサーバーへ置き換えるのが想定の流れ
    （docs/operations.md「バックアップ」参照）。
"""
from __future__ import annotations

import argparse
import gzip
import re
import shutil
import sqlite3
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np

from weatherlib.ncstore import DAILY_EPOCH, FILL, FILL_B, date_index, normalize_extents
from weatherlib.storelock import store_lock

BASE = Path(__file__).resolve().parent

# backfill_daily.py と同じ対応（値列, 品質列）
FIELD_MAP = {
    "tavg": ("平均気温", "平均気温注"),
    "tmax": ("最高気温", "最高気温注"),
    "tmin": ("最低気温", "最低気温注"),
    "precip": ("降水量", "降水量注"),
}
Q_VARS = {"tavg": "tavg_q", "tmax": "tmax_q", "tmin": "tmin_q", "precip": "precip_q"}

# 物理的にありえない閾値（×10 整数）。世界記録でも気温は -89.2〜56.7℃
TEMP_IMPOSSIBLE = 600          # |v| ≥ 60.0℃
# 日本記録（これを超えたら報告。消すかは人が決める）
JP_TMAX = 418                  # 41.8℃
JP_TMIN = -410                 # -41.0℃
# ダンプ末日（2022-03-14）の翌日。これ以降のありえない値はダンプ由来ではない
DUMP_END = date_index(date(2022, 3, 15))
# 同じ日に 10 行以上ありえない値 → 蓄積のゴミ列イベントとみなす
EVENT_MIN = 10


def log(msg: str) -> None:
    print(f"[repair] {msg}", flush=True)


def day_of(dj: int) -> date:
    return DAILY_EPOCH + timedelta(days=int(dj))


def cast16(v: int) -> int:
    """当時の書き込みと同じ int16 折り返し（int32 最小値 → 0 など）。"""
    return ((v + 32768) % 65536) - 32768


def to_int(s) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- ダンプ走査

def bad_cells_from_dump(path: Path):
    """修正後ガードなら捨てたはずの var → {(code, 日index): (折り返し値, 品質)}。

    weather.gz（全 DB の平文 pg_dump）の COPY jma_daily ブロックだけを読む。
    """
    opener = gzip.open if path.suffix == ".gz" else open
    bad: dict[str, dict[tuple[int, int], tuple[int, int | None]]] = {
        var: {} for var in FIELD_MAP}
    n_rows = 0
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        cols = None
        for line in f:
            if cols is None:
                m = re.match(r"COPY\s+\S*jma_daily\S*\s*\(([^)]+)\)\s+FROM stdin;", line)
                if m:
                    cols = [c.strip().strip('"') for c in m.group(1).split(",")]
                continue
            if line.startswith("\\."):
                break                    # jma_daily は 1 ブロックだけ
            vals = line.rstrip("\n").split("\t")
            r = {c: (None if v == "\\N" else v) for c, v in zip(cols, vals)}
            code = to_int(r.get("地点コード"))
            d_s = (r.get("観測日") or "")[:10]
            if code is None or len(d_s) != 10:
                continue
            n_rows += 1
            dj = date_index(date.fromisoformat(d_s))
            for var, (vcol, qcol) in FIELD_MAP.items():
                v = to_int(r.get(vcol))
                q = to_int(r.get(qcol))
                if v is None:
                    continue
                if v <= -999 or (q is not None and q < 1) or (var == "precip" and v < 0):
                    bad[var][(code, dj)] = (cast16(v), q)
    log(f"ダンプ: {n_rows:,} 行を走査、ガード違反 "
        + " ".join(f"{var} {len(c):,}" for var, c in bad.items()))
    return bad


# ---------------------------------------------------------------- 本体

def repair_var(ds, var: str, by_row: dict[int, dict[int, tuple[int, int | None]]],
               names: dict[int, str], apply: bool):
    """1 変数を行単位で修復する。戻り値 (修復セル数, ゴミ列イベントの日 index 集合)。"""
    v_val = ds[var]
    v_q = ds[Q_VARS[var]]
    nst = ds.dimensions["station"].size
    n_hit = n_inband = n_imp = moved = n_doubt = 0
    imp_by_dj: dict[int, int] = {}       # ダンプ期以降のありえない値の日別件数
    doubts: list[tuple[int, int, int, int]] = []

    for row in range(nst):
        vals = v_val[row, :]            # 行読みは詰め直しバグを踏まない
        qs = v_q[row, :]
        fix = np.zeros(len(vals), dtype=bool)

        # 1. ダンプ突き合わせ: 当時の値のまま残っているセルだけ
        for dj, (garbage, q) in by_row.get(row, {}).items():
            if dj >= len(vals) or vals[dj] == FILL:
                continue
            if vals[dj] != garbage:
                moved += 1               # 後続の蓄積が直した値。触らない
                continue
            # 折り返し値がもっともらしい範囲のときは品質の一致も求める
            # （本物の値を偶然の一致で消さないため）
            if abs(garbage) < TEMP_IMPOSSIBLE and not (var == "precip" and garbage < 0):
                if q is None or qs[dj] != np.int8(q):
                    continue
                n_inband += 1
            fix[dj] = True
            n_hit += 1

        # 2. ダンプに無い「ありえない値」の掃き出し（出所不明）
        if var == "precip":
            impossible = (vals != FILL) & (vals < 0) & ~fix
        else:
            impossible = (vals != FILL) & (np.abs(vals) >= TEMP_IMPOSSIBLE) & ~fix
        for dj in np.nonzero(impossible)[0]:
            if int(dj) >= DUMP_END:
                imp_by_dj[int(dj)] = imp_by_dj.get(int(dj), 0) + 1
            elif n_imp < 10:
                log(f"    出所不明（ダンプ期）: {names.get(row, '?')} {day_of(dj)} "
                    f"{var}={vals[dj] / 10:.1f} q={qs[dj]}")
            n_imp += 1
        fix |= impossible

        if apply and fix.any():
            vals[fix] = FILL
            qs[fix] = FILL_B
            v_val[row, :] = vals
            v_q[row, :] = qs

        # 3. 日本記録の外（報告候補。ゴミ列イベントの日は後段で選別される）
        if var != "precip":
            vv = np.where(fix, FILL, vals)
            doubt = (vv != FILL) & ((vv > JP_TMAX) | (vv < JP_TMIN)) \
                & (np.abs(vv) < TEMP_IMPOSSIBLE)
            for dj in np.nonzero(doubt)[0]:
                doubts.append((row, int(dj), int(vv[dj]), int(qs[dj])))

    n_fix = n_hit + n_imp
    events = {dj for dj, n in imp_by_dj.items() if n >= EVENT_MIN}
    for row, dj, v, q in doubts:
        if dj in events:
            continue                     # イベント日の破片は clean_extreme_events が消す
        log(f"    日本記録の外（残します）: {names.get(row, '?')} {day_of(dj)} "
            f"{var}={v / 10:.1f} q={q}")
        n_doubt += 1
    for dj in sorted(imp_by_dj):
        mark = "（ゴミ列イベント）" if dj in events else ""
        log(f"    蓄積期のありえない値: {day_of(dj)} {imp_by_dj[dj]} 行{mark}")
    log(f"{var}: 修復 {n_fix:,} セル（ダンプ一致 {n_hit:,}"
        f"（うち折り返しで範囲内 {n_inband}）・出所不明 {n_imp}）、"
        f"修正済みで残置 {moved:,}、記録外の報告 {n_doubt}")
    return n_fix, events


def clean_extreme_events(ds, var: str, events: set[int], names, apply: bool) -> int:
    """ゴミ列イベントの日の tmax/tmin から、範囲内に落ちた破片を選別して消す。

    蓄積の実行が読み出しバグ（ncstore.normalize_extents 参照）でゴミの混ざった
    列を書くと、ありえない値は前段で消えるが、偶然 ±60℃ に収まった破片が残る
    （51.3℃ や -47.0℃ など）。確定値 CSV 由来の本物のセルは品質 1〜8 を必ず
    持ち、起時は空欄（FILL）か 0〜1440 分なので、それ以外を欠測に戻す。
    precip は負値の掃き出しで足りる（集計由来の品質なし（-1）セルが正当に
    あるため、この選別は掛けない）。
    """
    if var not in ("tmax", "tmin") or not events:
        return 0
    v_val = ds[var]
    v_q = ds[Q_VARS[var]]
    v_min = ds[f"{var}_minutes"]
    n_cleaned = 0
    for dj in sorted(events):
        vals = v_val[:, dj]              # 列読みは詰め直しバグを踏まない
        qs = v_q[:, dj]
        mins = v_min[:, dj]
        ok = (np.abs(vals) < TEMP_IMPOSSIBLE) & (qs >= 1) & (qs <= 8) \
            & ((mins == FILL) | ((mins >= 0) & (mins <= 1440)))
        bad = (vals != FILL) & ~ok
        shard = bad & (np.abs(vals) < TEMP_IMPOSSIBLE)   # 範囲内の破片（報告対象）
        for row in np.nonzero(shard)[0][:10]:
            log(f"    破片: {names.get(int(row), '?')} {day_of(dj)} "
                f"{var}={vals[row] / 10:.1f} q={qs[row]} 起時={mins[row]}")
        n_cleaned += int(shard.sum())
        if apply and bad.any():
            vals[bad] = FILL
            qs[bad] = FILL_B
            mins[bad] = FILL
            v_val[:, dj] = vals
            v_q[:, dj] = qs
            v_min[:, dj] = mins
    log(f"{var}: ゴミ列イベント {len(events)} 日から範囲内の破片 {n_cleaned} セルを追加で欠測に")
    return n_cleaned


def verify(nc_path: Path):
    """修復後の検証: ありえない値が残っていないこと・極値が現実的なこと。"""
    import netCDF4 as nc
    ds = nc.Dataset(nc_path, "r")
    ds.set_auto_mask(False)
    nst = ds.dimensions["station"].size
    for var in FIELD_MAP:
        lo, hi, n_bad = 32767, -32768, 0
        for row in range(nst):
            vals = ds[var][row, :]
            vals = vals[vals != FILL]
            if not len(vals):
                continue
            lo, hi = min(lo, int(vals.min())), max(hi, int(vals.max()))
            if var == "precip":
                n_bad += int((vals < 0).sum())
            else:
                n_bad += int((np.abs(vals) >= TEMP_IMPOSSIBLE).sum())
        assert n_bad == 0, f"{var} にありえない値が {n_bad} 件残っています"
        unit = "mm" if var == "precip" else "℃"
        log(f"検証 {var}: {lo / 10:.1f} 〜 {hi / 10:.1f} {unit}")
    ds.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="欠測センチネルの修復（既定は下見だけ）")
    ap.add_argument("dump", type=Path, help="weather.gz（pg_dump 平文、gzip 可）")
    ap.add_argument("--store", type=Path, default=BASE / "store",
                    help="store ディレクトリ（既定 ./store。コピーに対して使うこと）")
    ap.add_argument("--apply", action="store_true", help="実際に書き換える")
    args = ap.parse_args()

    nc_path = args.store / "observations.nc"
    sq_path = args.store / "weather.sqlite"
    for p in (args.dump, nc_path, sq_path):
        if not p.exists():
            log(f"✗ ありません: {p}")
            return 1
    started = time.monotonic()

    bad = bad_cells_from_dump(args.dump)

    conn = sqlite3.connect(sq_path)
    code_row = dict(conn.execute("SELECT code, row FROM stations WHERE code IS NOT NULL"))
    names = dict(conn.execute("SELECT row, COALESCE(name, '?') FROM stations"))
    conn.close()

    # (code, dj) → (row ごとの dj 辞書) へ変換
    by_var_row: dict[str, dict[int, dict[int, tuple[int, int | None]]]] = {}
    unknown = set()
    for var, cells in bad.items():
        d: dict[int, dict[int, tuple[int, int | None]]] = {}
        for (code, dj), g in cells.items():
            row = code_row.get(code)
            if row is None:
                unknown.add(code)
                continue
            d.setdefault(row, {})[dj] = g
        by_var_row[var] = d
    if unknown:
        log(f"注意: ストアに無い地点コード {len(unknown)} 件（無視します）")

    import netCDF4 as nc
    work = nc_path.with_suffix(".nc.work")
    if args.apply:
        shutil.copy2(nc_path, work)
    ds = nc.Dataset(work if args.apply else nc_path, "a" if args.apply else "r")
    ds.set_auto_mask(False)

    total = 0
    for var in FIELD_MAP:
        n_fix, events = repair_var(ds, var, by_var_row[var], names, args.apply)
        total += n_fix
        total += clean_extreme_events(ds, var, events, names, args.apply)
    if args.apply:
        # 全変数の実寸を次元に揃える。これを済ませたストアは、生成側の
        # 広域読み（generate.py の [:, j0:j1] 等）が詰め直しバグを踏まない
        n = normalize_extents(ds)
        log(f"実寸を次元に揃えました: {n} 変数")
    ds.close()

    if args.apply:
        with store_lock(log=log):
            work.replace(nc_path)
        verify(nc_path)
        log(f"完了: {total:,} セルを FILL に戻しました "
            f"({time.monotonic() - started:.1f} 秒)")
    else:
        log(f"下見だけです（{total:,} セルが対象、--apply で書き換え。"
            f"{time.monotonic() - started:.1f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
