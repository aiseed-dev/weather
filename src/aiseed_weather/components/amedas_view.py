# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""AMeDAS ground observation map (the 今 panel of the 観測 tab).

Stays idle on mount; fetches only when the user presses 取得 / Fetch
(see the `user-action-fetch` skill). One snapshot carries every
element, so switching the element re-draws without fetching again.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC

import flet as ft

from aiseed_weather.components.observation_common import (
    JST,
    FigureSpec,
    Result,
    ResultArea,
    StatusArea,
    dropdown,
    fetch_buttons,
)
from aiseed_weather.figures.amedas_map import ELEMENTS
from aiseed_weather.figures.observation_chart import Provenance
from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services import jma_endpoints
from aiseed_weather.services.jma_amedas_service import JmaAmedasService

logger = logging.getLogger(__name__)

ATTRIBUTION = "出典: 気象庁ホームページ（編集・加工を行った旨と編集責任が利用者にあります）"


def _result(snapshot, stations, element: str, descending: bool) -> Result:
    el = ELEMENTS[element]
    rows = []
    for sid, obs in snapshot.observations.items():
        s = stations.get(sid)
        if s is None or element not in obs:
            continue
        rows.append((s, obs[element]))
    lons = tuple(s.longitude for s, _ in rows)
    lats = tuple(s.latitude for s, _ in rows)
    values = tuple(v for _, v in rows)
    valid = snapshot.timestamp
    jst = valid.astimezone(JST)
    spec = FigureSpec(
        kind="amedas", title=f"アメダス {el.label}", element=element, valid=valid,
        file_stem=f"amedas_{element}_{jst:%Y%m%d%H%M}",
        points=(lons, lats, values),
        prov=Provenance(data_source="JMA AMeDAS", period_id=f"{jst:%Y-%m-%d %H:%M} JST",
                        license_text=ATTRIBUTION),
    )
    ranked = sorted(rows, key=lambda r: (-r[1] if descending else r[1], r[0].station_id))
    table, prev, rank = [], None, 0
    for i, (s, v) in enumerate(ranked, 1):
        if v != prev:
            rank, prev = i, v
        table.append((str(rank), s.name_kanji, f"{v:g}", s.station_id))
    fetched = snapshot.fetched_at.astimezone(JST)
    return Result(
        spec=spec, png=b"",
        header=("順位", "地点", f"{el.label}（{el.unit}）", "アメダス番号"),
        rows=tuple(table),
        fetched_line=(f"観測 {jst:%Y-%m-%d %H:%M} JST（{valid.astimezone(UTC):%H:%M} UTC）"
                      f" · 取得 {fetched:%H:%M} JST · {jma_endpoints.ATTRIBUTION}"),
    )


@ft.component
def AmedasView(settings: UserSettings):
    state, set_state = ft.use_state("idle")
    progress, set_progress = ft.use_state("")
    error, set_error = ft.use_state(None)
    element, set_element = ft.use_state("temp")
    order, set_order = ft.use_state("desc")
    data, set_data = ft.use_state(None)          # (snapshot, stations)
    result, set_result = ft.use_state(None)

    async def draw(snapshot, stations, el: str, desc: bool):
        set_progress("Rendering station map…")
        r = _result(snapshot, stations, el, desc)
        png = await asyncio.to_thread(r.spec.render, 100)
        set_result(Result(spec=r.spec, png=png, header=r.header, rows=r.rows,
                          fetched_line=r.fetched_line))

    async def load(force: bool = False):
        set_state("loading")
        set_error(None)
        try:
            service = JmaAmedasService(data_dir=resolved_data_dir(settings))
            set_progress("Fetching AMeDAS snapshot from JMA…")
            snapshot = await service.fetch(force=force)
            set_progress("Reading the AMeDAS station table…")
            stations = await service.stations()
            set_data((snapshot, stations))
            await draw(snapshot, stations, element, order == "desc")
            set_state("ready")
        except Exception as e:
            logger.exception("AMeDAS view failed to fetch JMA snapshot")
            set_error(str(e))
            set_state("error")

    async def redraw(el: str, desc: bool):
        if data is None:
            return
        try:
            await draw(data[0], data[1], el, desc)
        except Exception as e:
            logger.exception("AMeDAS map render failed")
            set_error(str(e))
            set_state("error")

    def on_element(value: str):
        set_element(value)
        ft.context.page.run_task(redraw, value, order == "desc")

    def on_order(value: str):
        set_order(value)
        ft.context.page.run_task(redraw, element, value == "desc")

    return ft.Column(spacing=10, controls=[
        ft.Row(wrap=True, spacing=10, controls=[
            dropdown("要素", element, [(k, e.label) for k, e in ELEMENTS.items()], on_element,
                     width=210),
            dropdown("表の並び", order, [("desc", "高い順"), ("asc", "低い順")], on_order,
                     width=120),
            fetch_buttons(state, load),
        ]),
        StatusArea(state, progress, error,
                   "気象庁のアメダス（約 1,300 地点）の最新の 10 分値を地図と表にします。"),
        ResultArea(result) if state == "ready" and result else ft.Container(height=0),
    ])
