#!/usr/bin/env python3
"""地点別 JSON（エリアごと）から、ある時刻の全地点を組み立てる。

なぜこの層があるか
------------------
収集を map JSON（全地点 × 1 時刻）から地点別（1 地点 × 3 時間）へ移した。
形が転置しているので、そのままでは「ある時刻の全地点」を見る側が書き直しに
なる。ここで転置を吸収して map JSON と同じ形を返せば、消費側は入口を
差し替えるだけで済む。

    map JSON       {アメダス番号: {要素: [値, 品質]}}          ← 1 時刻
    地点別 JSON    {時刻: {要素: [値, 品質]}}                  ← 1 地点 3 時間
    エリア束        {アメダス番号: {時刻: {要素: [値, 品質]}}}  ← Worker が作る

置き場（tgsvr のローカル。R2 から取り寄せたもの）:
    public_amedas/point/{YYYYMMDD}/{HH}/{area_code}.json

地点別にして増えた要素: gust / gustDirection / gustTime / maxTemp / maxTempTime
/ minTemp / minTempTime。map JSON には無かった（2026-08-29 実測で 21 要素）。

prefNumber と observationNumber は [値, 品質] の対ではなく素の整数。
要素として扱うと品質判定で落ちるので、ここで除いておく。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
POINT = BASE / "public_amedas" / "point"
EXTRA = BASE / "public_amedas" / "extra"

# 地点別エンドポイントに無い要素を map から補う。
#
# **要素の一覧は持たない。** 欠測のときは要素そのものが来ないので、ある時刻に
# 無いことは「配信に無い」とも「いま欠測」とも取れる。一覧を決め打ちすると
# 判断を誤ったときに黙って欠ける。地点別を土台に、map にしか無かったものを
# そのまま重ねる形にして、気象庁が要素を足しても追随できるようにする。
#
# 2026-08-29 の実測（A〜G の 12 地点・同時刻）では、積雪 5 種と weather が
# map 側だけ、gust 3 種と maxTemp/minTemp 4 種が地点別だけにあった。
# weather は毎正時（分 00）のスロットにしか入らない。総称で重ねるので、
# 正時なら来る・それ以外は来ない、がそのまま反映される。

# 3 時間ブロックの開始時刻（気象庁のファイル名 {YYYYMMDD}_{HH}.json）
BLOCK_HOURS = ("00", "03", "06", "09", "12", "15", "18", "21")

# 観測要素ではないもの（[値, 品質] の対になっていない）
NOT_ELEMENTS = frozenset({"prefNumber", "observationNumber"})


def block_of(hour: int) -> str:
    """その時刻が属する 3 時間ブロックの名前。"""
    return f"{hour // 3 * 3:02d}"


def area_files(day: date, hour: str | None = None) -> list[Path]:
    """その日（必要ならそのブロック）のエリア束を返す。"""
    root = POINT / f"{day:%Y%m%d}"
    if not root.is_dir():
        return []
    blocks = [hour] if hour else BLOCK_HOURS
    out = []
    for b in blocks:
        d = root / b
        if d.is_dir():
            out.extend(sorted(d.glob("*.json")))
    return out


@lru_cache(maxsize=64)
def _load(path_str: str, mtime: float) -> dict:
    """エリア束を読む。mtime を鍵に含めるので、更新されれば読み直す。"""
    return json.loads(Path(path_str).read_bytes())


def load_area(path: Path) -> dict:
    try:
        return _load(str(path), path.stat().st_mtime)
    except Exception:
        return {}


def available_slots(day: date) -> list[str]:
    """その日に値のある 10 分スロットを古い順に返す（YYYYMMDDHHMM）。

    map 時代の load_slots() と同じ戻り値の形にしてある。
    """
    seen: set[str] = set()
    for p in area_files(day):
        for _amedas, series in load_area(p).items():
            seen.update(ts[:12] for ts in series)
    return sorted(seen)


def load_extra(ts: str) -> dict[str, dict]:
    """その時刻の補完分。map JSON から値のある要素だけ抜いたもの（無ければ空）。"""
    p = EXTRA / ts[:8] / f"{ts[:12]}.json"
    if not p.is_file():
        return {}
    try:
        return _load(str(p), p.stat().st_mtime)
    except Exception:
        return {}


def slot_view(ts: str) -> dict[str, dict]:
    """ある時刻の全地点を map JSON と同じ形で返す。

    ts は "YYYYMMDDHHMM"（12 桁）。地点別 JSON の鍵は 14 桁なので秒を補う。
    地点別に無い要素は map 由来のもので補う（合流はここだけで済ませ、
    消費側には「map と同じ形」だけを見せる）。
    """
    key = ts + "00" if len(ts) == 12 else ts
    day = date(int(key[:4]), int(key[4:6]), int(key[6:8]))
    out: dict[str, dict] = {}
    for p in area_files(day, block_of(int(key[8:10]))):
        for amedas, series in load_area(p).items():
            entry = series.get(key)
            if entry:
                out[amedas] = {k: v for k, v in entry.items() if k not in NOT_ELEMENTS}

    # map 由来を重ねる。地点別に既にある要素は上書きしない（地点別が主）。
    # 地点別が 404 等で丸ごと欠けた地点も、map だけで拾えるようにする。
    for amedas, extra in load_extra(key).items():
        entry = out.setdefault(amedas, {})
        for k, v in extra.items():
            entry.setdefault(k, v)
    return out


def hourly_view(ts: datetime) -> dict[str, dict]:
    """毎正時の蓄積用。jma.fetch_amedas_map() と同じ形で返す。

    戻り値: {アメダス番号: {'temp': ×10 or None, 'precip1h': ×10, 'sun1h': ×10}}
    品質フラグ 0（正常）のものだけを採る。accumulate.py が nc へ書く形。
    """
    def pick(entry, key):
        v = entry.get(key)
        if isinstance(v, list) and len(v) >= 2 and v[1] == 0 and v[0] is not None:
            return int(round(float(v[0]) * 10))
        return None

    out = {}
    for amedas, entry in slot_view(ts.strftime("%Y%m%d%H%M")).items():
        out[amedas] = {
            "temp": pick(entry, "temp"),
            "precip1h": pick(entry, "precipitation1h"),
            "sun1h": pick(entry, "sun1h"),
        }
    return out


def latest_slot(day: date) -> str | None:
    slots = available_slots(day)
    return slots[-1] if slots else None


def station_series(amedas: str, day: date) -> dict[str, dict]:
    """1 地点のその日ぶんを {時刻: {要素: [値, 品質]}} で返す（地点ページ用）。

    エリアが分かっていれば 1 ファイルで済むが、呼ぶ側に area_code を持たせると
    地点表の更新に追随させる手間が増えるので、その日のエリア束を舐める。
    """
    out: dict[str, dict] = {}
    for p in area_files(day):
        series = load_area(p).get(amedas)
        if series:
            for ts, entry in series.items():
                out[ts] = {k: v for k, v in entry.items() if k not in NOT_ELEMENTS}
    return dict(sorted(out.items()))
