"""帳簿ストア（SQLite: store/weather.sqlite）。

観測値本体は NetCDF（store/observations.nc、weatherlib/ncstore.py）に置き、
SQLite は関係データ（帳簿）だけを持つ。

地点の主キーは **code（地点番号）= 国際地点番号（官署）、無ければ etrn の 4 桁地点番号**。
これは気象庁の統計体系の番号で安定しており、**旧サイト DB の「地点コード」と同一体系**
（全国一意を検証済み: 4 桁側 2〜1678、官署側 47401〜47991）。

アメダス観測所番号は改番が多いため主キーにせず「現在の番号」という属性として持つ
（履歴は amedas_log）。observations.nc の行番号（row）は内部割当で不変。
新地点が etrn 未掲載で code が判明しない間は code=NULL で蓄積し、
build_master.py が etrn から解決して埋める。

  stations    … 地点マスタ（code=主キー〈契約上〉、row=nc 行番号、amedas=現在番号）
  amedas_log  … アメダス番号の履歴（row ごとの valid_from/valid_to）
  ingest_log  … 取り込み済み map ファイル等の記録（再取得防止）
  correction_log … 確定値 CSV の再取得で値が変わった（訂正された）記録
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
  row    INTEGER PRIMARY KEY,       -- observations.nc の station 行番号（内部割当・不変）
  code   INTEGER UNIQUE,            -- 地点番号（国際地点番号 or etrn 4桁番号）。契約上の主キー。
                                    -- etrn 未掲載の新地点は NULL（build_master が解決）
  amedas TEXT UNIQUE,               -- 現在のアメダス観測所番号（改番されうる属性）
  intl   INTEGER,                   -- 国際地点番号（官署のみ。code と一致）
  pref   TEXT,                      -- 都道府県（mdrr CSV 表記）
  name   TEXT,                      -- 地点名
  first_seen TEXT,                  -- 初めて観測データに現れた日時（JST）
  last_seen  TEXT                   -- 最後に観測データに現れた日時（JST）
);

CREATE TABLE IF NOT EXISTS amedas_log (
  row    INTEGER NOT NULL,          -- 対象地点（nc 行番号）
  amedas TEXT NOT NULL,             -- その期間のアメダス番号
  valid_from TEXT,                  -- この番号になった日時（NULL=蓄積開始時から）
  valid_to   TEXT                   -- 次の番号に変わった日時（NULL=現役）
);

CREATE TABLE IF NOT EXISTS ingest_log (
  kind TEXT NOT NULL,
  key  TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS correction_log (
  logged_at TEXT NOT NULL,
  amedas TEXT NOT NULL,
  date   TEXT NOT NULL,
  field  TEXT NOT NULL,
  old_value INTEGER,
  new_value INTEGER
);

CREATE TABLE IF NOT EXISTS votes_raw (
  ip_hash TEXT NOT NULL,            -- 投票者 IP の SHA-1（プライバシー配慮。生 IP は保存しない）
  date    TEXT NOT NULL,            -- 投票対象日
  code    INTEGER NOT NULL,         -- 地点番号
  v       INTEGER NOT NULL,         -- 1=合ってる / 0=違う
  PRIMARY KEY (ip_hash, date, code) -- 同一 IP の重複投票は最初の 1 票のみ
);
"""


def _table_cols(conn, name: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]


def _migrate(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "stations" not in tables:
        return
    cols = _table_cols(conn, "stations")

    if "nc_index" in cols:
        raise RuntimeError("v2 スキーマからの直接移行は未対応です（v3 を経由してください）")

    if "sid" in cols:  # v3（sid 主キー）→ v4（code 主キー・row 内部割当）
        # code は master/stations.json（etrn 突合済み）から引く
        code_by_sid: dict[int, int] = {}
        mp = Path(__file__).resolve().parent.parent / "master" / "stations.json"
        if mp.exists():
            m = json.loads(mp.read_text(encoding="utf-8"))["stations"]
            for sid_s, r in m.items():
                code = r.get("intl") or (int(r["etrn"]["block_no"]) if r.get("etrn") else None)
                if code is not None:
                    code_by_sid[int(sid_s)] = code

        conn.execute("ALTER TABLE stations RENAME TO stations_v3")
        conn.executescript(SCHEMA)
        for amedas, intl, pref, name, sid, fs, ls in conn.execute(
                "SELECT amedas, intl, pref, name, sid, first_seen, last_seen "
                "FROM stations_v3").fetchall():
            conn.execute(
                "INSERT INTO stations (row, code, amedas, intl, pref, name, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, code_by_sid.get(sid), amedas, intl, pref, name, fs, ls))
        if "station_code_log" in tables:
            conn.execute("INSERT INTO amedas_log (row, amedas, valid_from, valid_to) "
                         "SELECT sid, amedas, valid_from, valid_to FROM station_code_log")
            conn.execute("DROP TABLE station_code_log")
        conn.execute("DROP TABLE stations_v3")
        conn.commit()


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
