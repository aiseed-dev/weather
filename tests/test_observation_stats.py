# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import asyncio
import hashlib
from datetime import date

import numpy as np
import pytest

from aiseed_weather.services import observation_archive as oa
from aiseed_weather.services import observation_stats as st
from aiseed_weather.services.observation_archive import DailyBlock, ObsStation

NAN = np.nan
# 東京（府県番号 44）・熊谷（43）・稚内（11）・南鳥島（44, remote）
CODES = np.array([47662, 47626, 47401, 47991])


def _station(code, prec):
    return ObsStation(code=code, name=str(code), kana="", pref="", prec_no=prec, lat=0.0,
                      lon=0.0, alt=0.0, active=True, first=None, last=None)


STATIONS = {47662: _station(47662, 44), 47626: _station(47626, 43),
            47401: _station(47401, 11), 47991: _station(47991, 44)}


def _block(tmax, short=None, precip=None):
    tmax = np.array(tmax, dtype=np.float32)
    vals = {"tmax": tmax}
    sh = {"tmax": np.zeros(tmax.shape, bool) if short is None else np.array(short)}
    if precip is not None:
        vals["precip"] = np.array(precip, dtype=np.float32)
        sh["precip"] = np.zeros(tmax.shape, bool)
    return DailyBlock(codes=CODES, first=date(2018, 7, 21), vals=vals, short=sh)


def test_day_ranking_shares_ranks_and_orders_ties_north_to_south():
    b = _block([[36.0], [36.0], [36.0], [37.0]])
    rows = st.day_ranking(b, STATIONS, date(2018, 7, 21), st.Condition("tmax", ">=", 35))
    assert [(r.rank, r.code) for r in rows] == [(1, 47991), (2, 47401), (2, 47626), (2, 47662)]


def test_day_ranking_marks_short_values_and_excludes_remote_stations():
    short = [[False], [True], [False], [False]]
    b = _block([[36.0], [35.5], [20.0], [37.0]], short=short)
    rows = st.day_ranking(b, STATIONS, date(2018, 7, 21), st.Condition("tmax", ">=", 35),
                          exclude=st.REMOTE_STATIONS)
    assert [(r.code, r.short) for r in rows] == [(47662, False), (47626, True)]


def test_day_ranking_below_threshold_lists_lowest_first():
    b = _block([[5.0], [-3.0], [-8.0], [25.0]])
    rows = st.day_ranking(b, STATIONS, date(2018, 7, 21), st.Condition("tmax", "<", 0))
    assert [r.code for r in rows] == [47401, 47626]


def test_day_ranking_without_any_record_is_none_not_empty():
    b = _block([[NAN], [NAN], [NAN], [NAN]])
    assert st.day_ranking(b, STATIONS, date(2018, 7, 21), st.Condition("tmax", ">=", 35)) is None
    b = _block([[20.0], [21.0], [NAN], [NAN]])
    assert st.day_ranking(b, STATIONS, date(2018, 7, 21), st.Condition("tmax", ">=", 35)) == []


def test_period_summary_leaves_out_short_values_and_reports_latest_day_of_extreme():
    tmax = [[31.0, 33.0, 33.0, 30.0, 29.0],
            [36.0, 30.0, 30.0, 30.0, 30.0],
            [NAN, NAN, NAN, 20.0, 21.0],
            [30.0, 30.0, 30.0, 30.0, 30.0]]
    short = np.zeros((4, 5), bool)
    short[1, 0] = True                      # 熊谷の 36.0 は資料不足
    b = _block(tmax, short=short)
    rows = {r.code: r for r in st.period_summary(b, "tmax", st.Condition("tmax", ">=", 30))}
    assert 47401 not in rows               # 2 of 5 days < 80 % coverage
    assert rows[47662].max == 33.0 and rows[47662].max_on == date(2018, 7, 23)
    assert rows[47626].max == 30.0 and rows[47626].count == 4
    assert rows[47662].count == 4 and rows[47662].total is None


def test_period_summary_totals_precipitation():
    precip = [[0.0, 10.0, 5.5, 0.0, 0.0]] * 4
    b = _block([[30.0] * 5] * 4, precip=precip)
    rows = st.period_summary(b, "precip", None)
    assert all(abs(r.total - 15.5) < 1e-6 for r in rows) and rows[0].count is None


def test_rank_period_drops_zero_counts_and_ranks_both_ways():
    b = _block([[31.0, 31.0], [31.0, 20.0], [20.0, 20.0], [32.0, 33.0]])
    rows = st.period_summary(b, "tmax", st.Condition("tmax", ">=", 30))
    by_count = st.rank_period(rows, STATIONS, "count", descending=True)
    assert [(r, x.code) for r, x in by_count] == [(1, 47662), (1, 47991), (3, 47626)]
    by_min = st.rank_period(rows, STATIONS, "min", descending=False)
    assert [x.code for _, x in by_min][0] == 47401


def _write_year(path, year, codes, tmax, precip, precip_none, q):
    import netCDF4
    n = tmax.shape[1]
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("station", len(codes))
        ds.createDimension("time", n)
        ds.createVariable("station_code", "i4", ("station",))[:] = codes
        ds.createVariable("time", "i4", ("time",))[:] = \
            np.arange(n) + (date(year, 1, 1) - oa.EPOCH).days
        for v, a in (("tmax", tmax), ("tmin", tmax), ("tavg", tmax), ("precip", precip),
                     ("sun", tmax)):
            var = ds.createVariable(v, "i2", ("station", "time"), fill_value=np.int16(-32768))
            var.scale_factor = 0.1
            var[:] = a
        ds.createVariable("tmax_q", "i1", ("station", "time"), fill_value=np.int8(-1))[:] = q
        ds.createVariable("precip_none", "i1", ("station", "time"),
                          fill_value=np.int8(-1))[:] = precip_none


def test_read_years_concatenates_and_reads_no_precipitation_as_zero(tmp_path):
    codes = np.array([1, 2])
    for y in (2024, 2025):
        n = 366 if y == 2024 else 365
        tmax = np.full((2, n), 25.0)
        precip = np.ma.masked_array(np.zeros((2, n)), mask=True)
        none = np.zeros((2, n), dtype=np.int8)
        none[0, :] = 1                        # station 1: "--"（降水なし）
        q = np.full((2, n), 8, dtype=np.int8)
        q[1, -1] = 4
        _write_year(tmp_path / f"{y}.nc", y, codes, tmax, precip, none, q)
    b = oa._read_years([tmp_path / "2024.nc", tmp_path / "2025.nc"],
                       date(2024, 12, 30), date(2025, 1, 2), ("tmax", "precip"))
    assert b.n_days == 4 and b.first == date(2024, 12, 30)
    assert np.allclose(b.vals["tmax"], 25.0)
    assert np.all(b.vals["precip"][0] == 0.0) and np.isnan(b.vals["precip"][1]).all()
    assert b.short["tmax"][1, 1] and not b.short["tmax"][0].any()


def test_year_file_with_matching_sha_is_not_downloaded_again(tmp_path, monkeypatch):
    archive = oa.ObservationArchive(data_dir=tmp_path)
    cached = tmp_path / "observations" / "daily" / "years" / "2018.nc"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"netcdf")
    sha = hashlib.sha256(b"netcdf").hexdigest()
    m = oa._parse_manifest({"coverage": "1880-01-01..2026-10-04",
                            "years": [{"year": 2018, "path": "years/2018.nc", "sha256": sha,
                                       "bytes": 6}],
                            "stations": []}, None)

    async def no_download(url, dst):
        raise AssertionError("must not download")
    monkeypatch.setattr(archive, "_download", no_download)
    assert asyncio.run(archive.year_path(2018, m)) == cached
    assert asyncio.run(archive.year_path(1999, m)) is None


def test_condition_label():
    assert st.Condition("tmin", "<", 0).label() == "日最低気温 < 0 °C"
    with pytest.raises(KeyError):
        st.Condition("snow", ">=", 1).label()
