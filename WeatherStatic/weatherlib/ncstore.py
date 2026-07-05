"""観測値ストア（NetCDF-4 単一ファイル: store/observations.nc）。

STORAGE_FORMATS.md 案 B の実装。
  - 観測値本体はすべてこのファイル（station × time / station × date の int16 配列）
  - 帳簿（地点⇔行番号の割当・取込ログ・訂正ログ）は SQLite（store/weather.sqlite）

配列設計:
  次元:  station(unlimited), time(unlimited: 毎正時), date(unlimited: 日)
  時間軸は「エポックからの線形インデックス」= 配列添字。
    time index = (JST 時刻 - 2020-01-01T00:00) の時間数
    date index = (日付 - 1870-01-01) の日数         ※過去バックフィル余地を確保
  未使用領域は HDF5 のチャンク割当が起きないため容量を消費しない。
  値は ×10 整数（int16）。欠測は FILL(-32768)。品質・回数は int8、欠測 -1。
  起時は「0 時からの分」（int16）。

クラッシュ耐性:
  呼び出し側（accumulate.py）が「コピー → 更新 → rename」で運用する。
"""
from __future__ import annotations

import numpy as np
import netCDF4 as nc
from datetime import date, datetime, timedelta
from pathlib import Path

FILL = np.int16(-32768)
FILL_B = np.int8(-1)

HOURLY_EPOCH = datetime(2020, 1, 1, 0, 0)   # JST
DAILY_EPOCH = date(1870, 1, 1)

# (変数名, 次元種別, 型, 説明)
_HOURLY_VARS = [
    ("temp", "i2", "hourly air temperature x10 (0.1 degC)"),
    ("precip1h", "i2", "hourly precipitation x10 (0.1 mm)"),
    ("sun1h", "i2", "hourly sunshine duration x10 (0.1 h)"),
]
_DAILY_VARS = [
    ("tmax", "i2", "daily max temperature x10"),
    ("tmax_minutes", "i2", "time of daily max, minutes from midnight"),
    ("tmax_q", "i1", "quality of daily max (JMA code)"),
    ("tmin", "i2", "daily min temperature x10"),
    ("tmin_minutes", "i2", "time of daily min, minutes from midnight"),
    ("tmin_q", "i1", "quality of daily min (JMA code)"),
    ("tavg", "i2", "daily mean temperature x10 (avg of 24 hourly)"),
    ("tavg_count", "i1", "number of hourly values used for tavg"),
    ("tavg_q", "i1", "quality of daily mean (JMA code; backfill)"),
    ("precip", "i2", "daily precipitation x10"),
    ("precip_q", "i1", "quality of daily precipitation (JMA code; backfill)"),
    ("precip_none", "i1", "no-precipitation flag (JMA; backfill)"),
    ("sun", "i2", "daily sunshine x10"),
]


def hour_index(ts: datetime) -> int:
    return int((ts - HOURLY_EPOCH).total_seconds() // 3600)


def date_index(d: date) -> int:
    return (d - DAILY_EPOCH).days


def minutes_of(hhmm: str) -> int:
    """'13:48' → 828。'24:00' も許容。空は FILL。"""
    if not hhmm or ":" not in hhmm:
        return int(FILL)
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class NcStore:
    """observations.nc のラッパ。

    station 行番号（row）は SQLite stations の内部割当（不変・使い回しなし）。
    契約上の地点主キーは code（国際地点番号 or etrn 4 桁番号）で、code → row の
    対応は stations テーブルが持つ。アメダス番号は改番されうる属性
    （改番時は同じ row に蓄積が継続し、番号履歴は amedas_log に残る）。
    """

    def __init__(self, path: Path, sqlite_conn, mode: str = "a"):
        self.conn = sqlite_conn
        create = not Path(path).exists()
        self.ds = nc.Dataset(path, "w" if create else mode, format="NETCDF4")
        self.ds.set_auto_mask(False)
        if create:
            self._create_schema()
        elif mode == "a":
            self._ensure_vars()
        # 現在のアメダス番号 → row（SQLite が正）
        self.sidx: dict[str, int] = dict(self.conn.execute(
            "SELECT amedas, row FROM stations WHERE amedas IS NOT NULL"))

    def _ensure_vars(self):
        """スキーマ拡張時の追随: 既存ファイルに不足している日別変数を追加する。"""
        common = dict(zlib=True, complevel=5, shuffle=True)
        for name, typ, desc in _DAILY_VARS:
            if name not in self.ds.variables:
                v = self.ds.createVariable(name, typ, ("station", "date"),
                                           fill_value=FILL if typ == "i2" else FILL_B,
                                           chunksizes=(256, 366), **common)
                v.long_name = desc

    def _create_schema(self):
        ds = self.ds
        ds.Conventions = "CF-1.10"
        ds.featureType = "timeSeries"
        ds.title = "AMeDAS observations accumulated for weather site"
        ds.createDimension("station", None)
        ds.createDimension("time", None)
        ds.createDimension("date", None)
        v = ds.createVariable("station_id", str, ("station",))
        v.long_name = "AMeDAS station number"
        v = ds.createVariable("time", "i4", ("time",), fill_value=-1)
        v.units = f"hours since {HOURLY_EPOCH:%Y-%m-%d %H:%M} +09:00"
        v = ds.createVariable("date", "i4", ("date",), fill_value=-1)
        v.units = f"days since {DAILY_EPOCH:%Y-%m-%d}"
        common = dict(zlib=True, complevel=5, shuffle=True)
        for name, typ, desc in _HOURLY_VARS:
            v = ds.createVariable(name, typ, ("station", "time"),
                                  fill_value=FILL if typ == "i2" else FILL_B,
                                  chunksizes=(256, 720), **common)
            v.long_name = desc
        for name, typ, desc in _DAILY_VARS:
            v = ds.createVariable(name, typ, ("station", "date"),
                                  fill_value=FILL if typ == "i2" else FILL_B,
                                  chunksizes=(256, 366), **common)
            v.long_name = desc

    # ---------------------------------------------------------- station 行番号

    def station_index(self, amedas: str) -> int:
        """このアメダス番号の地点の row。未知の番号なら新しい row を割り当てて記録する。

        code（地点番号）は etrn 掲載まで NULL のまま（build_master.py が解決して埋める）。
        注意: 改番（既存地点の番号変更）はここでは検知できず新地点扱いになる。
        改番が判明したら build_master.py --renumber で row/code を維持したまま番号を付け替える。
        """
        row = self.sidx.get(amedas)
        if row is not None:
            return row
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(row), -1) + 1 FROM stations")
        row = cur.fetchone()[0]
        self.conn.execute(
            "INSERT INTO stations (row, amedas) VALUES (?, ?)", (row, amedas))
        self.conn.execute(
            "INSERT INTO amedas_log (row, amedas, valid_from, valid_to) "
            "VALUES (?, ?, datetime('now', 'localtime'), NULL)", (row, amedas))
        self.ds["station_id"][row] = amedas
        self.sidx[amedas] = row
        return row

    def _nst(self) -> int:
        return self.ds.dimensions["station"].size

    def _column(self, values: dict[str, int | None], typ=np.int16, fill=FILL) -> np.ndarray:
        """{amedas: 値} を station 全行の列ベクトルにする（先に行番号を割当てる）。"""
        for a in values:
            self.station_index(a)
        col = np.full(self._nst(), fill, dtype=typ)
        for a, v in values.items():
            if v is not None:
                col[self.sidx[a]] = v
        return col

    # ---------------------------------------------------------- hourly

    def write_hour(self, ts: datetime, data: dict[str, dict]) -> None:
        """毎正時 1 本分を書く。data = {amedas: {'temp':…, 'precip1h':…, 'sun1h':…}}"""
        j = hour_index(ts)
        for name, _, _ in _HOURLY_VARS:
            col = self._column({a: v.get(name) for a, v in data.items()})
            self.ds[name][: len(col), j] = col
        self.ds["time"][j] = j

    # ---------------------------------------------------------- daily

    def update_daily_extreme(self, d: date, kind: str,
                             data: dict[str, tuple[int, str, int]]) -> list[tuple]:
        """確定値 CSV 由来の tmax/tmin を upsert する。

        kind: 'tmax' or 'tmin'。data = {amedas: (値, 起時 'HH:MM', 品質)}
        戻り値: 訂正一覧 [(amedas, old, new), ...]
        """
        j = date_index(d)
        for a in data:
            self.station_index(a)
        nst = self._nst()

        v_val = self.ds[kind]
        old = v_val[:nst, j] if self.ds.dimensions["date"].size > j else np.full(nst, FILL)
        corrections = []
        val_col = old.copy() if len(old) == nst else np.full(nst, FILL, np.int16)
        min_col = np.full(nst, FILL, np.int16)
        q_col = np.full(nst, FILL_B, np.int8)
        # 既存の分・品質は保持したいので読み出し（date 次元が既にあれば）
        if self.ds.dimensions["date"].size > j:
            min_col = self.ds[f"{kind}_minutes"][:nst, j]
            q_col = self.ds[f"{kind}_q"][:nst, j]
            if len(min_col) != nst:
                min_col = np.concatenate([min_col, np.full(nst - len(min_col), FILL, np.int16)])
                q_col = np.concatenate([q_col, np.full(nst - len(q_col), FILL_B, np.int8)])
        if len(val_col) != nst:
            val_col = np.concatenate([val_col, np.full(nst - len(val_col), FILL, np.int16)])

        for a, (val, at, q) in data.items():
            i = self.sidx[a]
            if val_col[i] != FILL and val_col[i] != val:
                corrections.append((a, int(val_col[i]), val))
            val_col[i] = val
            min_col[i] = minutes_of(at)
            q_col[i] = q
        v_val[:nst, j] = val_col
        self.ds[f"{kind}_minutes"][:nst, j] = min_col
        self.ds[f"{kind}_q"][:nst, j] = q_col
        self.ds["date"][j] = j
        return corrections

    def aggregate_day(self, d: date) -> int:
        """hourly から日平均気温（1〜24 時の毎正時平均）・日降水量・日照を集計して daily に書く。"""
        j0 = hour_index(datetime(d.year, d.month, d.day, 1, 0))   # 1 時
        j1 = j0 + 24                                              # 翌日 0 時まで（24 本）
        if self.ds.dimensions["time"].size < j1:
            return 0
        nst = self._nst()
        temp = self.ds["temp"][:nst, j0:j1]
        prec = self.ds["precip1h"][:nst, j0:j1]
        sun = self.ds["sun1h"][:nst, j0:j1]

        t_ok = temp != FILL
        t_cnt = t_ok.sum(axis=1).astype(np.int8)
        t_sum = np.where(t_ok, temp.astype(np.int64), 0).sum(axis=1)
        with np.errstate(invalid="ignore"):
            mean = t_sum / np.maximum(t_cnt, 1)
        tavg = np.where(t_cnt > 0,
                        np.where(mean >= 0, (mean + 0.5).astype(np.int16),
                                 -((-mean + 0.5).astype(np.int16))),
                        FILL).astype(np.int16)

        p_ok = prec != FILL
        p_sum = np.where(p_ok.any(axis=1),
                         np.where(p_ok, prec.astype(np.int64), 0).sum(axis=1), FILL).astype(np.int16)
        s_ok = sun != FILL
        s_sum = np.where(s_ok.any(axis=1),
                         np.where(s_ok, sun.astype(np.int64), 0).sum(axis=1), FILL).astype(np.int16)

        jd = date_index(d)
        self.ds["tavg"][:nst, jd] = tavg
        self.ds["tavg_count"][:nst, jd] = t_cnt
        self.ds["precip"][:nst, jd] = p_sum
        self.ds["sun"][:nst, jd] = s_sum
        self.ds["date"][jd] = jd
        return int((t_cnt > 0).sum())

    def close(self):
        self.ds.close()
