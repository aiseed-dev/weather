# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""観測 / Observations — JMA observations, now and since 1880.

Four panels behind one mode switch:

* 今 (AMeDAS) — the latest 10-minute snapshot, straight from JMA.
* 日のランキング — stations meeting a condition on one day.
* 期間の集計 — a statistic of every station over any period.
* 地点の推移 — one station's daily values over a period.

The last three read the redistributed daily records
(``services/observation_archive``); the arithmetic is in
``services/observation_stats``. Every panel is idle until the user
presses 取得 (see the `user-action-fetch` skill), shows what period it
covers and when it was fetched, and saves its figure as PNG with the
provenance footer, re-rendered at export resolution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import flet as ft
import numpy as np

from aiseed_weather.components.amedas_view import AmedasView
from aiseed_weather.components.observation_common import (
    FigureSpec,
    Result,
    ResultArea,
    StatusArea,
    dropdown,
    fetch_buttons,
    fetched_line,
    field,
    parse_date,
    parse_float,
    provenance,
    station_label,
    yesterday_jst,
)
from aiseed_weather.figures.observation_chart import bar_colour
from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services import observation_endpoints as ep
from aiseed_weather.services import observation_stats as st
from aiseed_weather.services.observation_archive import ObservationArchive

logger = logging.getLogger(__name__)

CHART_TOP = 30              # bars in the figure; the table lists everything
MAX_PERIOD_YEARS = 20       # all stations × days held in memory

MODES = (
    ("now", "今（アメダス）"),
    ("day", "日のランキング"),
    ("period", "期間の集計"),
    ("station", "地点の推移"),
)
ELEMENT_OPTIONS = [(k, v[0]) for k, v in st.ELEMENT_INFO.items()]
OP_OPTIONS = [(">=", "以上"), ("<", "未満")]


# ── 日のランキング ──────────────────────────────────────────────────


@ft.component
def DayRankingPanel(data_dir: Path):
    day, set_day = ft.use_state(lambda: yesterday_jst().isoformat())
    element, set_element = ft.use_state("tmax")
    op, set_op = ft.use_state(">=")
    threshold, set_threshold = ft.use_state("35")
    exclude_remote, set_exclude_remote = ft.use_state(True)
    state, set_state = ft.use_state("idle")
    progress, set_progress = ft.use_state("")
    error, set_error = ft.use_state(None)
    result, set_result = ft.use_state(None)

    async def run(force: bool = False):
        try:
            d = parse_date(day)
            cond = st.Condition(element, op, parse_float(threshold, "閾値"))
        except ValueError as e:
            set_error(str(e))
            set_state("error")
            return
        set_state("loading")
        set_error(None)
        try:
            archive = ObservationArchive(data_dir=data_dir)
            block, m = await archive.load_period(d, d, elements=(element,), force=force,
                                                 progress=set_progress)
            stations = await archive.stations()
            set_progress("Ranking stations…")
            exclude = st.REMOTE_STATIONS if exclude_remote else frozenset()
            rows = st.day_ranking(block, stations, d, cond, exclude=exclude)
            if rows is None:
                raise ValueError(f"{d} の{st.ELEMENT_INFO[element][0]}の記録はありません")
            name, unit = st.ELEMENT_INFO[element]
            title = f"{d:%Y年%-m月%-d日} {cond.label()}の地点（{len(rows)} 地点）"
            spec = FigureSpec(
                kind="ranking", title=title, file_stem=f"{element}{'ge' if op == '>=' else 'lt'}"
                f"{threshold}_{d:%Y%m%d}",
                subtitle=f"上位 {min(CHART_TOP, len(rows))} 地点。「]」は資料不足値"
                         + ("。南鳥島・富士山を除く" if exclude_remote else ""),
                unit=f"{name}（{unit}）", colour=bar_colour(element, op == ">="),
                entries=tuple((r.rank, station_label(stations, r.code), r.value, r.short)
                              for r in rows if r.rank <= CHART_TOP),
                zero_based=element in ("precip", "sun"),
                prov=provenance(m, d.isoformat()),
            )
            set_progress("Rendering figure…")
            png = await asyncio.to_thread(spec.render, 100)
            set_result(Result(
                spec=spec, png=png, header=("順位", "地点", f"{name}（{unit}）", "地点番号"),
                rows=tuple((str(r.rank), station_label(stations, r.code),
                            f"{r.value:.1f}" + (" ]" if r.short else ""), str(r.code))
                           for r in rows),
                fetched_line=fetched_line(m, d.isoformat()),
            ))
            set_state("ready")
        except Exception as e:
            logger.exception("Day ranking failed")
            set_error(str(e))
            set_state("error")

    return ft.Column(spacing=10, controls=[
        ft.Row(wrap=True, spacing=10, controls=[
            field("日付", day, set_day, hint="YYYY-MM-DD"),
            dropdown("要素", element, ELEMENT_OPTIONS, set_element),
            field("閾値", threshold, set_threshold, width=90),
            dropdown("条件", op, OP_OPTIONS, set_op, width=110),
            ft.Checkbox(label="南鳥島・富士山を除く", value=exclude_remote,
                        on_change=lambda e: set_exclude_remote(bool(e.control.value))),
            fetch_buttons(state, run),
        ]),
        StatusArea(state, progress, error,
                   "その日に条件を満たした全国の地点を、値の順に並べます（1880 年〜）。"),
        ResultArea(result) if state == "ready" and result else ft.Container(height=0),
    ])


# ── 期間の集計 ──────────────────────────────────────────────────────


@ft.component
def PeriodPanel(data_dir: Path):
    def default_start():
        y = yesterday_jst()
        return y.replace(day=1).isoformat()

    start, set_start = ft.use_state(default_start)
    end, set_end = ft.use_state(lambda: yesterday_jst().isoformat())
    metric, set_metric = ft.use_state("count")
    element, set_element = ft.use_state("tmax")
    op, set_op = ft.use_state(">=")
    threshold, set_threshold = ft.use_state("30")
    order, set_order = ft.use_state("desc")
    exclude_remote, set_exclude_remote = ft.use_state(False)
    state, set_state = ft.use_state("idle")
    progress, set_progress = ft.use_state("")
    error, set_error = ft.use_state(None)
    result, set_result = ft.use_state(None)

    async def run(force: bool = False):
        try:
            d0, d1 = parse_date(start), parse_date(end)
            if d1 < d0:
                raise ValueError("終わりの日が始まりの日より前です")
            if d1.year - d0.year >= MAX_PERIOD_YEARS:
                raise ValueError(f"期間は {MAX_PERIOD_YEARS} 年までにしてください")
            cond = (st.Condition(element, op, parse_float(threshold, "閾値"))
                    if metric == "count" else None)
            if metric == "total" and element not in ("precip", "sun"):
                raise ValueError("合計は降水量と日照時間だけです")
        except ValueError as e:
            set_error(str(e))
            set_state("error")
            return
        set_state("loading")
        set_error(None)
        try:
            archive = ObservationArchive(data_dir=data_dir)
            block, m = await archive.load_period(d0, d1, elements=(element,), force=force,
                                                 progress=set_progress)
            stations = await archive.stations()
            set_progress("Summarising every station…")
            exclude = st.REMOTE_STATIONS if exclude_remote else frozenset()
            rows = await asyncio.to_thread(st.period_summary, block, element, cond, exclude=exclude)
            ranked = st.rank_period(rows, stations, metric, descending=order == "desc")
            first, last = block.first, block.date_at(block.n_days - 1)
            name, unit = st.ELEMENT_INFO[element]
            what = cond.label() + "の日数" if cond else f"{name}の{st.PERIOD_METRICS[metric]}"
            covers = f"{first}〜{last}"
            way = "多い" if metric == "count" else "高い" if order == "desc" else "低い"
            title = f"{covers} {what}（{way}順）"

            def value_of(r) -> float:
                return float(getattr(r, metric))

            fmt = "{:.0f}" if metric == "count" else "{:.1f}"
            spec = FigureSpec(
                kind="ranking", title=title,
                file_stem=f"{element}-{metric}_{first:%Y%m%d}-{last:%Y%m%d}",
                subtitle=(f"上位 {CHART_TOP} 位。期間の 8 割以上の日に値がある {len(rows)} 地点が"
                          "対象。資料不足値は含めない"),
                unit="日" if metric == "count" else f"{name}（{unit}）",
                colour=bar_colour(element, order == "desc"),
                entries=tuple((rk, station_label(stations, r.code), value_of(r), False)
                              for rk, r in ranked if rk <= CHART_TOP),
                value_format=fmt, zero_based=metric == "count" or element in ("precip", "sun"),
                prov=provenance(m, f"{first}..{last}"),
            )
            set_progress("Rendering figure…")
            png = await asyncio.to_thread(spec.render, 100)
            with_day = metric in ("max", "min")
            header = ("順位", "地点", "日数" if metric == "count" else f"{name}（{unit}）") \
                + (("起日",) if with_day else ()) + ("値のある日数", "地点番号")
            table = []
            for rk, r in ranked:
                cells = [str(rk), station_label(stations, r.code), fmt.format(value_of(r))]
                if with_day:
                    cells.append(str(r.max_on if metric == "max" else r.min_on))
                cells += [str(r.days_with_data), str(r.code)]
                table.append(tuple(cells))
            set_result(Result(spec=spec, png=png, header=header, rows=tuple(table),
                              fetched_line=fetched_line(m, covers),
                              note="同じ値が何日もあるときは新しい方の日を起日にしています。"
                              if with_day else ""))
            set_state("ready")
        except Exception as e:
            logger.exception("Period summary failed")
            set_error(str(e))
            set_state("error")

    controls = [
        field("始まり", start, set_start, hint="YYYY-MM-DD"),
        field("終わり", end, set_end, hint="YYYY-MM-DD"),
        dropdown("集計", metric, list(st.PERIOD_METRICS.items()), set_metric, width=230),
        dropdown("要素", element, ELEMENT_OPTIONS, set_element),
    ]
    if metric == "count":
        controls += [field("閾値", threshold, set_threshold, width=90),
                     dropdown("条件", op, OP_OPTIONS, set_op, width=110)]
    controls += [
        dropdown("並び", order, [("desc", "多い・高い順"), ("asc", "少ない・低い順")], set_order),
        ft.Checkbox(label="南鳥島・富士山を除く", value=exclude_remote,
                    on_change=lambda e: set_exclude_remote(bool(e.control.value))),
        fetch_buttons(state, run),
    ]
    return ft.Column(spacing=10, controls=[
        ft.Row(wrap=True, spacing=10, controls=controls),
        StatusArea(state, progress, error,
                   f"任意の期間（{MAX_PERIOD_YEARS} 年まで）について、全国の地点の日数・極値・"
                   "平均・合計を並べます。"),
        ResultArea(result) if state == "ready" and result else ft.Container(height=0),
    ])


# ── 地点の推移 ──────────────────────────────────────────────────────


@ft.component
def StationPanel(data_dir: Path):
    query, set_query = ft.use_state("東京")
    start, set_start = ft.use_state(lambda: (yesterday_jst() - timedelta(days=364)).isoformat())
    end, set_end = ft.use_state(lambda: yesterday_jst().isoformat())
    state, set_state = ft.use_state("idle")
    progress, set_progress = ft.use_state("")
    error, set_error = ft.use_state(None)
    result, set_result = ft.use_state(None)
    candidates, set_candidates = ft.use_state(())

    async def show(code: int, force: bool = False):
        try:
            d0, d1 = parse_date(start), parse_date(end)
            if d1 < d0:
                raise ValueError("終わりの日が始まりの日より前です")
        except ValueError as e:
            set_error(str(e))
            set_state("error")
            return
        set_state("loading")
        set_error(None)
        set_candidates(())
        try:
            archive = ObservationArchive(data_dir=data_dir)
            stations = await archive.stations(force=force)
            series, m = await archive.load_station(code, force=force, progress=set_progress)
            j0 = max(0, (d0 - series.first).days)
            j1 = min(len(series.vals["tmax"]), (d1 - series.first).days + 1)
            if j1 <= j0:
                raise ValueError("その期間の記録はありません")
            first = series.first + timedelta(days=j0)
            last = series.first + timedelta(days=j1 - 1)
            vals = {k: v[j0:j1].astype(np.float64) for k, v in series.vals.items()}
            stat = {k: np.where(series.short[k][j0:j1], np.nan, v) for k, v in vals.items()}
            covers = f"{first}〜{last}"
            label = station_label(stations, code)
            spec = FigureSpec(kind="series",
                              title=f"{label}（{code}）の日々の気温と降水量 {covers}",
                              file_stem=f"station{code}_{first:%Y%m%d}-{last:%Y%m%d}",
                              prov=provenance(m, f"{first}..{last}"),
                              series_first=first, series=vals)
            set_progress("Rendering figure…")
            png = await asyncio.to_thread(spec.render, 100)

            def summary(el: str) -> tuple[str, ...]:
                x = stat[el]
                ok = ~np.isnan(x)
                name, unit = st.ELEMENT_INFO[el]
                if not ok.any():
                    return (f"{name}（{unit}）", "—", "", "—", "", "—", "—", "0")
                jmax = len(x) - 1 - int(np.nanargmax(x[::-1]))
                jmin = len(x) - 1 - int(np.nanargmin(x[::-1]))
                total = f"{x[ok].sum():.1f}" if el in ("precip", "sun") else "—"
                return (f"{name}（{unit}）", f"{x[jmax]:.1f}", str(first + timedelta(days=jmax)),
                        f"{x[jmin]:.1f}", str(first + timedelta(days=jmin)),
                        f"{x[ok].mean():.1f}", total, str(int(ok.sum())))

            set_result(Result(
                spec=spec, png=png,
                header=("要素", "最高", "起日", "最低", "起日", "平均", "合計", "値のある日数"),
                rows=tuple(summary(el) for el in st.ELEMENT_INFO),
                fetched_line=fetched_line(m, covers),
                note="表は期間の統計（資料不足値を除く）。同じ値が何日もあるときは新しい方の日を起日にしています。",
            ))
            set_state("ready")
        except Exception as e:
            logger.exception("Station series failed")
            set_error(str(e))
            set_state("error")

    async def run(force: bool = False):
        q = query.strip()
        if not q:
            set_error("地点名か地点番号を入力してください")
            set_state("error")
            return
        set_state("loading")
        set_error(None)
        set_progress("Reading the station list…")
        try:
            stations = await ObservationArchive(data_dir=data_dir).stations(force=force)
        except Exception as e:
            logger.exception("Station list failed")
            set_error(str(e))
            set_state("error")
            return
        if q.isdigit() and int(q) in stations:
            matches = [stations[int(q)]]
        else:
            exact = [s for s in stations.values() if s.name == q]
            matches = exact or [s for s in stations.values() if q in s.name or q in s.kana]
        if not matches:
            set_error(f"「{q}」に当たる地点がありません")
            set_state("error")
            return
        if len(matches) == 1:
            await show(matches[0].code, force)
            return
        matches.sort(key=lambda s: (not s.active, s.prec_no, s.code))
        set_candidates(tuple((s.code, f"{s.pref} {s.name}{'' if s.active else '（廃止）'}")
                             for s in matches[:40]))
        set_state("idle")

    choose = ft.Column(spacing=6, controls=[
        ft.Text("地点を選んでください（同じ名前・似た名前の地点があります）", size=12),
        ft.Row(wrap=True, spacing=6, controls=[
            ft.OutlinedButton(label, on_click=lambda _, c=code: ft.context.page.run_task(show, c))
            for code, label in candidates
        ]),
    ]) if candidates else ft.Container(height=0)

    return ft.Column(spacing=10, controls=[
        ft.Row(wrap=True, spacing=10, controls=[
            field("地点（名前か番号）", query, set_query, width=180),
            field("始まり", start, set_start, hint="YYYY-MM-DD"),
            field("終わり", end, set_end, hint="YYYY-MM-DD"),
            fetch_buttons(state, run),
        ]),
        choose,
        StatusArea(state, progress, error if not candidates else None,
                   "1 地点の日々の気温と降水量を、任意の期間で図にします（廃止された地点も含む）。")
        if not candidates else ft.Container(height=0),
        ResultArea(result) if state == "ready" and result else ft.Container(height=0),
    ])


# ── shell ───────────────────────────────────────────────────────────


@ft.component
def ObservationView(settings: UserSettings):
    mode, set_mode = ft.use_state("day")
    data_dir = resolved_data_dir(settings)

    def mode_button(key: str, label: str) -> ft.Control:
        if key == mode:
            return ft.FilledButton(label, on_click=lambda _, k=key: set_mode(k))
        return ft.OutlinedButton(label, on_click=lambda _, k=key: set_mode(k))

    body = {
        "now": lambda: AmedasView(settings=settings),
        "day": lambda: DayRankingPanel(data_dir=data_dir),
        "period": lambda: PeriodPanel(data_dir=data_dir),
        "station": lambda: StationPanel(data_dir=data_dir),
    }[mode]()

    return ft.Container(padding=16, content=ft.Column(
        scroll=ft.ScrollMode.AUTO, expand=True, spacing=12, controls=[
            ft.Text("観測 / Observations (JMA)", size=18, weight=ft.FontWeight.BOLD),
            ft.Row([mode_button(k, label) for k, label in MODES], wrap=True, spacing=8),
            body,
            ft.Text(f"{ep.ATTRIBUTION}。日別の記録は個人開発気象統計が配っている NetCDF"
                    f"（{ep.BASE}/data/daily/）から取得します。",
                    size=10, color=ft.Colors.GREY),
        ],
    ))
