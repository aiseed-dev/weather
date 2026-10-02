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

読みは行単位か 1 セル単位で行う。libnetcdf（4.9.2〜4.10.1 で確認）には、
変数自身の書込み済み範囲より unlimited 次元が大きいとき、範囲を越える読みが
「ずれた配列＋未初期化メモリ」を返すバグがある。常に正しいのは 1 セル読みと
1 行読みだけ（詳細は ncstore.normalize_extents と tests/test_nc_read_bug.py）。
蓄積がこのバグで書いたゴミ列（2026-08-26〜29 の tmax/tmin など）も、
ここで一緒に掃除する。

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

class Truth:
    """ダンプ期（〜2022-03-14）に、修正後の取込が書いたはずの値・品質・降水なし。"""

    def __init__(self, nst: int):
        shape = (nst, DUMP_END)
        self.v = {var: np.full(shape, FILL, np.int16) for var in FIELD_MAP}
        self.q = {var: np.full(shape, FILL_B, np.int8) for var in FIELD_MAP}
        self.pn = np.full(shape, FILL_B, np.int8)


def bad_cells_from_dump(path: Path, code_row: dict[int, int], nst: int):
    """ダンプを 1 回走査して (ガード違反セル, Truth) を返す。

    ガード違反セル: 修正後ガードなら捨てたはずの
        var → {(code, 日index): (折り返し値, 品質)}
    weather.gz（全 DB の平文 pg_dump）の COPY jma_daily ブロックだけを読む。
    ダンプに (地点, 日) の重複行は無い（2026-10-03 確認）ので先勝ちは考えない。
    """
    opener = gzip.open if path.suffix == ".gz" else open
    bad: dict[str, dict[tuple[int, int], tuple[int, int | None]]] = {
        var: {} for var in FIELD_MAP}
    truth = Truth(nst)
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
            row = code_row.get(code)
            known = row is not None and row < nst and dj < DUMP_END
            for var, (vcol, qcol) in FIELD_MAP.items():
                v = to_int(r.get(vcol))
                q = to_int(r.get(qcol))
                if v is None:
                    continue
                if v <= -999 or (q is not None and q < 1) or (var == "precip" and v < 0):
                    bad[var][(code, dj)] = (cast16(v), q)
                elif known:
                    truth.v[var][row, dj] = v
                    if q is not None:
                        truth.q[var][row, dj] = q
            pn = to_int(r.get("降水無"))
            if known and pn is not None:
                truth.pn[row, dj] = pn      # 取込は値の可否と無関係に書く
    log(f"ダンプ: {n_rows:,} 行を走査、ガード違反 "
        + " ".join(f"{var} {len(c):,}" for var, c in bad.items()))
    return bad, truth


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
    nst = ds.dimensions["station"].size

    def column(v, dj):
        # 列読みは日付方向の実寸より外だと壊れるので、常に正しい 1 セル読みで組む
        return np.array([v[r, dj] for r in range(nst)], dtype=v.dtype)

    for dj in sorted(events):
        vals = column(v_val, dj)
        qs = column(v_q, dj)
        mins = column(v_min, dj)
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


def rebuild_flags(ds, truth: Truth, apply: bool) -> None:
    """降水なしフラグ（precip_none）をダンプから作り直し、品質（*_q）を点検する。

    初回のダンプ取込（backfill_daily.py の年ブロック読み）も同じ読み出しバグを
    踏み、当時まだ誰も書いていなかった tavg_q / precip_q / precip_none の
    ブロックを乱数のまま書き戻した。値のあるセルの品質はダンプの品質で上書き
    されたので正しい（値のないセルの乱数は clean_orphans が消す）。
    precip_none は「空いているときだけ書く」なので、乱数に阻まれて本物が
    入っていない。配布パック（export_dist.py）に出る変数なので直す。

    ダンプ期:
      品質  … 書き換えない。値がダンプどおりで品質だけ違うセルは、etrn が
              速報値の品質（4/5）を確定（8）に更新したもの（2022-03 の 37 セル）
      降水なし … ダンプの値に合わせる。ダンプに行が無い所は欠測。
              etrn が降水量を書き換えたセルで 0/1 が入っていればそれを残す
    ダンプ期より後:
      降水なし … etrn は「降水なし」のとき降水量 0 と一緒に 1 を書くだけなので、
              降水量 0 かつ 1 のセルだけ残し、ほかは欠測に戻す
    """
    nst = ds.dimensions["station"].size
    for var in FIELD_MAP:                        # 品質は点検だけ（書き換えない）
        v_val, v_q = ds[var], ds[Q_VARS[var]]
        n = 0
        for row in range(nst):
            vals = v_val[row, :DUMP_END]         # 1 行読みは常に正しい
            qs = v_q[row, :DUMP_END]
            intact = (vals != FILL) & (vals == truth.v[var][row])
            n += int((intact & (qs != truth.q[var][row])).sum())
        log(f"{var}: 値はダンプどおりで品質だけ違うセル {n:,}（etrn の確定品質。残します）")

    v_p, v_pn = ds["precip"], ds["precip_none"]
    n_dump = n_after = 0
    for row in range(nst):
        pv = v_p[row, :]
        pn = v_pn[row, :]
        new = pn.copy()
        etrn = (pv[:DUMP_END] != FILL) & (pv[:DUMP_END] != truth.v["precip"][row]) \
            & ((pn[:DUMP_END] == 0) | (pn[:DUMP_END] == 1))
        new[:DUMP_END] = np.where(etrn, pn[:DUMP_END], truth.pn[row])
        new[DUMP_END:] = np.where((pn[DUMP_END:] == 1) & (pv[DUMP_END:] == 0), 1, FILL_B)
        n_dump += int((new[:DUMP_END] != pn[:DUMP_END]).sum())
        n_after += int((new[DUMP_END:] != pn[DUMP_END:]).sum())
        if apply and (new != pn).any():
            v_pn[row, :] = new
    log(f"precip_none: ダンプ期 {n_dump:,} セル・それ以降 {n_after:,} セルを作り直し")


def clean_orphans(ds, apply: bool) -> int:
    """値が欠測なのに品質・起時だけ残っているセル（孤児）を欠測に揃える。

    どの書き手（ダンプ取込・確定値 CSV・etrn）も品質と起時は値と一緒にしか
    書かないので、「値が FILL なら品質・起時も FILL」は不変条件。ゴミ列は
    値がたまたま FILL でも品質・起時にゴミを残す（2026-08-26 がこの形）。
    etrn の再取得は値と品質しか書かないため、起時のゴミは先に消しておく。
    下見では、この実行で消す予定のセルぶんは数に入らない（既存の孤児だけ）。
    """
    nst = ds.dimensions["station"].size
    total = 0
    for var in FIELD_MAP:
        v_val, v_q = ds[var], ds[Q_VARS[var]]
        v_min = ds[f"{var}_minutes"] if f"{var}_minutes" in ds.variables else None
        n = 0
        for row in range(nst):
            gone = v_val[row, :] == FILL
            qs = v_q[row, :]
            orphan_q = gone & (qs != FILL_B)
            mins = orphan_m = None
            if v_min is not None:
                mins = v_min[row, :]
                orphan_m = gone & (mins != FILL)
            n += int((orphan_q | orphan_m).sum() if orphan_m is not None else orphan_q.sum())
            if apply and orphan_q.any():
                qs[orphan_q] = FILL_B
                v_q[row, :] = qs
            if apply and orphan_m is not None and orphan_m.any():
                mins[orphan_m] = FILL
                v_min[row, :] = mins
        log(f"{var}: 値なしで品質・起時だけ残る孤児 {n:,} セルを欠測に")
        total += n
    return total


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
    ds = nc.Dataset(nc_path, "r")
    ds.set_auto_mask(False)
    left = clean_orphans(ds, apply=False)
    ds.close()
    assert left == 0, f"孤児セルが {left} 件残っています"


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

    conn = sqlite3.connect(sq_path)
    code_row = dict(conn.execute("SELECT code, row FROM stations WHERE code IS NOT NULL"))
    names = dict(conn.execute("SELECT row, COALESCE(name, '?') FROM stations"))
    conn.close()

    import netCDF4 as nc
    with nc.Dataset(nc_path, "r") as probe:
        nst = probe.dimensions["station"].size
    bad, truth = bad_cells_from_dump(args.dump, code_row, nst)

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
    rebuild_flags(ds, truth, args.apply)
    n_orphan = clean_orphans(ds, args.apply)
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
        log(f"完了: 値 {total:,} セルを FILL に戻し、孤児 {n_orphan:,} セルを掃除しました "
            f"({time.monotonic() - started:.1f} 秒)")
    else:
        log(f"下見だけです（値 {total:,} セルが対象、既存の孤児 {n_orphan:,} セル。"
            f"--apply で書き換え。{time.monotonic() - started:.1f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
