#!/usr/bin/env python3
"""改番の row 引き継ぎを確かめる。

ncstore.station_index は未知の番号に**新しい row** を割り当てる。改番のたびに
新地点として切れると、1880 年からの統計の連続性が壊れる。record_renumber が
row を保ったまま番号だけ付け替えられるかを見る。
"""
import sqlite3, sys
from pathlib import Path

WS = Path.home() / "dev/weather/WeatherStatic"
sys.path.insert(0, str(WS))
DB = WS / "store/weather.sqlite"

con = sqlite3.connect(DB)
OLD = "11001"                      # 宗谷岬
NEW = "11002"                      # 改番先（架空）

row, = con.execute("SELECT row FROM stations WHERE amedas = ?", (OLD,)).fetchone()
n_before = con.execute("SELECT count(*) FROM amedas_log WHERE row = ?", (row,)).fetchone()[0]
print(f"改番前: {OLD} は row={row} / amedas_log {n_before} 行")

# observations.nc のその行に値があることを確かめる（連続性の対象）
import numpy as np, netCDF4 as nc
ds = nc.Dataset(WS / "store/observations.nc"); ds.set_auto_mask(False)
vals = ds.variables["tmax"][row, :]
n_valid = int((vals != -32768).sum())
ds.close()
print(f"  observations.nc の row {row}: 有効 {n_valid:,} 日分")

con.close()

import station_changes
station_changes.record_renumber(OLD, NEW)

con = sqlite3.connect(DB)
after = con.execute("SELECT row FROM stations WHERE amedas = ?", (NEW,)).fetchone()
old_gone = con.execute("SELECT row FROM stations WHERE amedas = ?", (OLD,)).fetchone()
logs = con.execute(
    "SELECT amedas, valid_from, valid_to FROM amedas_log WHERE row = ? ORDER BY rowid",
    (row,)).fetchall()
print(f"\n改番後: {NEW} は row={after[0] if after else None}")
print(f"  {OLD} は stations から消えた: {old_gone is None}")
print(f"  amedas_log:")
for a, f, t in logs:
    print(f"    {a}  from={f}  to={t}")

ok = (after and after[0] == row and old_gone is None
      and len(logs) == n_before + 1
      and any(a == OLD and t is not None for a, _f, t in logs)
      and any(a == NEW and t is None for a, _f, t in logs))
print(f"\nrow を保ったまま付け替え、有効期間も記録: {ok}")

# 後始末
con.execute("UPDATE stations SET amedas = ? WHERE row = ?", (OLD, row))
con.execute("DELETE FROM amedas_log WHERE row = ? AND amedas = ?", (row, NEW))
con.execute("UPDATE amedas_log SET valid_to = NULL WHERE row = ? AND amedas = ?",
            (row, OLD))
con.commit(); con.close()
print("後始末: 帳簿を元に戻した")
sys.exit(0 if ok else 1)
