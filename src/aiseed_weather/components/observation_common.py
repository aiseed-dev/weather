# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Pieces shared by the 観測 (observations) panels: form fields, the
fetch buttons, the status line, and the result area (figure, PNG
export at print resolution, TSV copy, table)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import flet as ft
import numpy as np

from aiseed_weather.figures.amedas_map import render_amedas_map
from aiseed_weather.figures.observation_chart import (
    Provenance,
    render_ranking,
    render_station_series,
)
from aiseed_weather.services import observation_endpoints as ep
from aiseed_weather.services.observation_archive import Manifest, ObsStation

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
TABLE_LIMIT = 300           # rows rendered on screen (the TSV copy has all)
EXPORT_DPI = 200


def file_picker() -> ft.FilePicker:
    """Page-level FilePicker singleton (no Control in component state)."""
    page = ft.context.page
    services = list(getattr(page, "services", None) or [])
    for s in services:
        if isinstance(s, ft.FilePicker):
            return s
    fp = ft.FilePicker()
    services.append(fp)
    page.services = services
    return fp


def yesterday_jst() -> date:
    return datetime.now(JST).date() - timedelta(days=1)


def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        raise ValueError(f"日付は YYYY-MM-DD で入力してください: {s!r}") from None


def parse_float(s: str, what: str) -> float:
    try:
        return float(s.strip())
    except ValueError:
        raise ValueError(f"{what}は数で入力してください: {s!r}") from None


def station_label(stations: dict[int, ObsStation], code: int) -> str:
    s = stations.get(code)
    return f"{s.pref} {s.name}" if s else str(code)


def provenance(m: Manifest, period_id: str) -> Provenance:
    return Provenance(data_source=ep.SOURCE_NAME, period_id=period_id,
                      license_text="出典: 気象庁ホームページ（気象庁のデータを編集・加工）")


def fetched_line(m: Manifest, covers: str) -> str:
    fetched = m.fetched_at.astimezone(JST) if m.fetched_at.tzinfo else m.fetched_at
    return (f"記録: {covers}（JST の暦日）· 公開範囲 {m.coverage_first}〜{m.coverage_last}"
            f" · 目録の取得 {fetched:%Y-%m-%d %H:%M} JST")


@dataclass(frozen=True)
class FigureSpec:
    """What a figure needs, kept as data so export can re-render it."""

    kind: str                  # "ranking" | "series" | "amedas"
    title: str
    file_stem: str
    prov: Provenance
    subtitle: str = ""
    unit: str = ""
    colour: str = ""
    entries: tuple = ()
    value_format: str = "{:.1f}"
    zero_based: bool = True
    series_first: date | None = None
    series: dict | None = None
    points: tuple = ()             # amedas: (lons, lats, values) as tuples
    element: str = ""
    valid: datetime | None = None

    def render(self, dpi: int) -> bytes:
        if self.kind == "amedas":
            lons, lats, values = (np.asarray(a, dtype=float) for a in self.points)
            return render_amedas_map(lons, lats, values, element=self.element, valid=self.valid,
                                     attribution=self.prov.license_text, dpi=dpi)
        if self.kind == "ranking":
            return render_ranking(list(self.entries), title=self.title, subtitle=self.subtitle,
                                  unit=self.unit, colour=self.colour, prov=self.prov,
                                  value_format=self.value_format,
                                  zero_based=self.zero_based, dpi=dpi)
        return render_station_series(self.series_first, self.series, title=self.title,
                                     prov=self.prov, dpi=dpi)


@dataclass(frozen=True)
class Result:
    spec: FigureSpec
    png: bytes
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    fetched_line: str
    note: str = ""

    def tsv(self) -> str:
        lines = ["\t".join(self.header)] + ["\t".join(r) for r in self.rows]
        lines.append(f"# {self.spec.prov.license_text} / {self.spec.prov.data_source}")
        return "\n".join(lines)


def dropdown(label: str, value: str, options, on_select, width: int = 150) -> ft.Dropdown:
    return ft.Dropdown(
        label=label, value=value, width=width, dense=True,
        options=[ft.dropdown.Option(key=k, text=t) for k, t in options],
        on_select=lambda e: on_select(e.control.value),
    )


def field(label: str, value: str, on_change, width: int = 140,
          hint: str | None = None) -> ft.TextField:
    return ft.TextField(label=label, value=value, width=width, dense=True, hint_text=hint,
                        on_change=lambda e: on_change(e.control.value))


@ft.component
def StatusArea(state: str, progress: str, error: str | None, idle_text: str):
    if state == "loading":
        return ft.Row([ft.ProgressRing(width=18, height=18),
                       ft.Text(progress or "Loading…", color=ft.Colors.GREY)], spacing=10)
    if state == "error":
        return ft.Text(f"取得できませんでした: {error}", color=ft.Colors.RED)
    if state == "idle":
        return ft.Text(idle_text, color=ft.Colors.GREY, size=12)
    return ft.Container(height=0)


@ft.component
def ResultArea(result: Result):
    """Figure, provenance line, save / copy actions and the full table."""
    message, set_message = ft.use_state("")

    async def save_png():
        set_message("")
        try:
            chosen = await file_picker().save_file(
                dialog_title="図を PNG で保存",
                file_name=f"aiseed-weather_{result.spec.file_stem}.png",
                allowed_extensions=["png"],
            )
        except Exception as exc:
            logger.exception("save_file dialog failed")
            set_message(f"保存に失敗しました: {type(exc).__name__}: {exc}")
            return
        if not chosen:
            return
        try:
            png = await asyncio.to_thread(result.spec.render, EXPORT_DPI)
            await asyncio.to_thread(Path(chosen).write_bytes, png)
            logger.info("Observation figure saved → %s (%.1f KB)", chosen, len(png) / 1024)
            set_message(f"保存しました: {chosen}")
        except Exception as exc:
            logger.exception("Observation PNG export failed")
            set_message(f"保存に失敗しました: {type(exc).__name__}: {exc}")

    def copy_tsv(_e):
        try:
            ft.context.page.set_clipboard(result.tsv())
            set_message(f"コピーしました（{len(result.rows)} 行・TSV）")
        except Exception as exc:
            logger.exception("Clipboard copy failed")
            set_message(f"コピーに失敗しました: {type(exc).__name__}")

    shown = result.rows[:TABLE_LIMIT]
    table = ft.DataTable(
        columns=[ft.DataColumn(ft.Text(h, weight=ft.FontWeight.BOLD)) for h in result.header],
        rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(c)) for c in r]) for r in shown],
        heading_row_height=34, data_row_min_height=26, data_row_max_height=30,
        column_spacing=18,
    )
    return ft.Column(spacing=8, controls=[
        ft.Text(result.fetched_line, size=11, color=ft.Colors.GREY),
        ft.Row([
            ft.OutlinedButton("PNG で保存", icon=ft.Icons.SAVE_ALT,
                              on_click=lambda _: ft.context.page.run_task(save_png)),
            ft.OutlinedButton("表をコピー（TSV）", icon=ft.Icons.CONTENT_COPY, on_click=copy_tsv),
            ft.Text(message, size=12, color=ft.Colors.GREY),
        ], wrap=True),
        ft.Image(src=result.png, fit=ft.BoxFit.CONTAIN),
        ft.Text(result.note, size=11, color=ft.Colors.GREY) if result.note
        else ft.Container(height=0),
        ft.Row([table], scroll=ft.ScrollMode.AUTO),
        ft.Text(f"表は上位 {TABLE_LIMIT} 行まで表示（コピーは全 {len(result.rows)} 行）",
                size=11, color=ft.Colors.GREY) if len(result.rows) > TABLE_LIMIT
        else ft.Container(height=0),
    ])


def fetch_buttons(state: str, run) -> ft.Control:
    """取得 when idle; 再取得 (bypassing the cache) once there is a result."""
    if state == "loading":
        return ft.FilledButton("取得中…", disabled=True)
    if state in ("ready", "error"):
        return ft.Row([
            ft.FilledButton("取得 / Fetch", on_click=lambda _: ft.context.page.run_task(run)),
            ft.OutlinedButton("再取得 / Refresh",
                              on_click=lambda _: ft.context.page.run_task(run, True)),
        ])
    return ft.FilledButton("取得 / Fetch", on_click=lambda _: ft.context.page.run_task(run))


