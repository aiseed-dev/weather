# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import flet as ft

from aiseed_weather.components.amedas_view import AmedasView
from aiseed_weather.components.map_view import MapView
from aiseed_weather.components.radar_view import RadarView
from aiseed_weather.models import user_settings
from aiseed_weather.models.user_settings import LoadResult


@ft.component
def App():
    # Config is read once at startup. Editing the TOML and restarting is the
    # only way to change sources — see the `first-run-setup` skill.
    result, _ = ft.use_state(user_settings.load_or_init())
    active_view, set_active_view = ft.use_state("map")

    # ──────────────────────────────────────────────────────────
    # App-level GPV fetch session.
    #
    # Lives at App scope (not MapView) so the running download
    # survives tab navigation. The Map view kicks off fetches but
    # the lifecycle is owned here; the global banner above the nav
    # shows progress + stop button from any tab.
    # ──────────────────────────────────────────────────────────
    fetch_running, set_fetch_running = ft.use_state(False)
    fetch_progress, set_fetch_progress = ft.use_state(
        lambda: {"done": 0, "total": 0}
    )
    fetch_status_text, set_fetch_status_text = ft.use_state("")
    # Per-frame fetch items so the Fetch tab can render pip install-
    # style detailed progress: status icon + label + size + duration.
    # Each item is a dict:
    #   {"step": int, "label": str, "param": str, "stitched": bool,
    #    "status": "pending"|"checking"|"downloading"|"done"|"cached"
    #              |"failed"|"cancelled",
    #    "size_bytes": int|None, "duration_s": float|None}
    fetch_items, set_fetch_items = ft.use_state(lambda: [])
    # Mutable holder for the asyncio task + cancel_event. use_state
    # with a lazy initializer returns the same dict object across
    # renders, so writes survive.
    fetch_task_ref, _ = ft.use_state(
        lambda: {"task": None, "cancel_event": None}
    )

    def stop_fetch():
        ev = fetch_task_ref.get("cancel_event")
        if ev is not None:
            ev.set()
        task = fetch_task_ref.get("task")
        if task is not None and not task.done():
            task.cancel()
        fetch_task_ref["task"] = None
        fetch_task_ref["cancel_event"] = None
        set_fetch_running(False)
        set_fetch_status_text("")

    fetch_session = {
        "running": fetch_running,
        "set_running": set_fetch_running,
        "progress": fetch_progress,
        "set_progress": set_fetch_progress,
        "status_text": fetch_status_text,
        "set_status_text": set_fetch_status_text,
        "items": fetch_items,
        "set_items": set_fetch_items,
        "task_ref": fetch_task_ref,
        "stop": stop_fetch,
    }

    if result.status != "ok":
        return ft.SafeArea(expand=True, content=ConfigStatusPanel(result=result))

    settings = result.settings
    body = {
        "map": lambda: MapView(settings=settings, fetch_session=fetch_session),
        "radar": lambda: RadarView(settings=settings),
        "amedas": lambda: AmedasView(settings=settings),
    }[active_view]()

    # ──────────────────────────────────────────────────────────
    # VS Code-style tabbed bottom panel.
    #
    # The header row (tabs + chevron) is always visible above the
    # NavigationBar. The panel body expands below when the user
    # clicks a tab or the chevron. Fetch progress + Stop sit in the
    # header so they're reachable even when the panel is collapsed
    # OR when the user is on a different tab.
    #
    # Tabs:
    #   - 取得状況 / Fetch    GPV download status, stop button,
    #                          per-frame log
    #   - ターミナル / Terminal  Python REPL for ad-hoc workflows
    #                            (ERA5 anomaly composites etc.) —
    #                            placeholder for now
    #   - ログ / Logs         tail of recent app log lines
    #                          (placeholder)
    # ──────────────────────────────────────────────────────────
    bottom_panel_open, set_bottom_panel_open = ft.use_state(False)
    bottom_panel_tab, set_bottom_panel_tab = ft.use_state(0)

    # Auto-open the panel + switch to the Fetch tab the moment a
    # background download starts, so the user sees what's happening
    # without having to hunt for it.
    def _autoopen_on_fetch_start():
        if fetch_running:
            set_bottom_panel_tab(0)
            set_bottom_panel_open(True)
    ft.use_effect(_autoopen_on_fetch_start, [fetch_running])

    done = fetch_progress.get("done", 0)
    total = fetch_progress.get("total", 0)

    def _tab_button(label: str, idx: int) -> ft.Control:
        is_active = bottom_panel_tab == idx
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=4,
            bgcolor=(
                ft.Colors.SURFACE_CONTAINER_HIGHEST if is_active
                else None
            ),
            on_click=lambda _, i=idx: (
                set_bottom_panel_tab(i),
                set_bottom_panel_open(True),
            ),
            content=ft.Text(
                label,
                size=12,
                weight=ft.FontWeight.BOLD if is_active else ft.FontWeight.NORMAL,
            ),
        )

    fetch_tab_label = (
        f"取得状況 / Fetch  ({done}/{total})"
        if fetch_running else "取得状況 / Fetch"
    )

    bottom_panel_header = ft.Container(
        height=32,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Row(
                    spacing=4,
                    controls=[
                        _tab_button(fetch_tab_label, 0),
                        _tab_button("ターミナル / Terminal", 1),
                        _tab_button("ログ / Logs", 2),
                    ],
                ),
                ft.Row(
                    spacing=4,
                    controls=[
                        # Stop visible in the header whenever a fetch
                        # is running, regardless of which tab is open
                        # or whether the body is expanded.
                        ft.IconButton(
                            icon=ft.Icons.STOP_CIRCLE,
                            icon_size=18,
                            tooltip="停止 / Stop fetch",
                            visible=fetch_running,
                            on_click=lambda _: stop_fetch(),
                        ),
                        ft.IconButton(
                            icon=(
                                ft.Icons.KEYBOARD_ARROW_DOWN
                                if bottom_panel_open
                                else ft.Icons.KEYBOARD_ARROW_UP
                            ),
                            icon_size=18,
                            tooltip=(
                                "下部パネルを閉じる"
                                if bottom_panel_open
                                else "下部パネルを開く"
                            ),
                            on_click=lambda _: set_bottom_panel_open(
                                not bottom_panel_open,
                            ),
                        ),
                    ],
                ),
            ],
        ),
    )

    # ── Tab body: 取得状況 (pip install-style detailed view) ──
    def _format_size(b):
        if b is None:
            return "—"
        if b < 1024:
            return f"{b} B"
        if b < 1024 * 1024:
            return f"{b / 1024:.0f} KB"
        return f"{b / (1024 * 1024):.1f} MB"

    def _format_duration(s):
        if s is None:
            return "—"
        if s < 1:
            return f"{int(s * 1000)} ms"
        if s < 60:
            return f"{s:.1f} s"
        return f"{int(s // 60)}m{int(s % 60):02d}s"

    _STATUS_GLYPH = {
        "pending": ("·", ft.Colors.OUTLINE),
        "checking": ("…", ft.Colors.AMBER),
        "downloading": ("↓", ft.Colors.PRIMARY),
        "done": ("✓", ft.Colors.GREEN),
        "cached": ("◎", ft.Colors.GREEN_300),
        "failed": ("✗", ft.Colors.RED),
        "cancelled": ("−", ft.Colors.OUTLINE),
    }

    # Aggregate stats for the header.
    items = fetch_items
    n_done = sum(1 for it in items if it["status"] in ("done", "cached"))
    n_failed = sum(1 for it in items if it["status"] == "failed")
    n_total = len(items)
    total_bytes = sum(
        (it["size_bytes"] or 0) for it in items
        if it["status"] in ("done", "cached")
    )
    # ETA estimate: mean of completed real-download durations
    # (excludes cache hits since those are ~instant).
    real_durations = [
        it["duration_s"] for it in items
        if it["status"] == "done" and it.get("duration_s")
    ]
    pending_count = sum(
        1 for it in items
        if it["status"] in ("pending", "checking", "downloading")
    )
    if real_durations and pending_count > 0:
        avg_s = sum(real_durations) / len(real_durations)
        eta_str = _format_duration(avg_s * pending_count)
    else:
        eta_str = "—"

    # One row per frame. Compact, fixed-width columns aligned by setting
    # explicit widths so the eye can scan down a single property.
    def _item_row(it):
        glyph, color = _STATUS_GLYPH.get(it["status"], ("?", ft.Colors.GREY))
        return ft.Row(
            spacing=8,
            controls=[
                ft.Text(glyph, color=color, size=12, width=14),
                ft.Text(
                    f"T+{it['step']}h", size=11, width=72,
                    font_family="monospace",
                ),
                ft.Text(
                    (
                        it["param"]
                        + (f"  [ext]" if it.get("stitched") else "")
                    ),
                    size=10, color=ft.Colors.GREY, width=110,
                    font_family="monospace",
                ),
                ft.Text(
                    _format_size(it.get("size_bytes")), size=10, width=72,
                    color=ft.Colors.GREY,
                ),
                ft.Text(
                    _format_duration(it.get("duration_s")), size=10, width=70,
                    color=ft.Colors.GREY,
                ),
                ft.Text(
                    it["status"], size=10, color=color, expand=True,
                ),
            ],
        )

    fetch_tab_body = ft.Container(
        padding=ft.Padding.all(12),
        content=ft.Column(
            spacing=6,
            expand=True,
            controls=[
                ft.Row(
                    spacing=12,
                    controls=[
                        ft.Text(
                            (
                                "GPV 取得中" if fetch_running
                                else (
                                    "現在の取得タスクなし"
                                    if n_total == 0
                                    else f"取得完了 ({n_done}/{n_total})"
                                )
                            ),
                            size=13, weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            f"{n_done}/{n_total} frames",
                            size=11, color=ft.Colors.GREY,
                        ),
                        ft.Text(
                            f"DL 累計: {_format_size(total_bytes)}",
                            size=11, color=ft.Colors.GREY,
                        ),
                        ft.Text(
                            f"残り推定: {eta_str}",
                            size=11, color=ft.Colors.GREY,
                            visible=(pending_count > 0),
                        ),
                        ft.Text(
                            f"失敗: {n_failed}",
                            size=11, color=ft.Colors.RED,
                            visible=(n_failed > 0),
                        ),
                    ],
                ),
                ft.ProgressBar(
                    value=(n_done / n_total) if n_total else 0,
                    color=ft.Colors.PRIMARY,
                    visible=(n_total > 0),
                ),
                ft.Text(
                    fetch_status_text or "—",
                    size=10, color=ft.Colors.GREY,
                    visible=bool(fetch_status_text),
                ),
                ft.Divider(height=4),
                # Scrollable list of per-frame rows. Empty state hint
                # when there's no items (e.g. cold start before first fetch).
                ft.ListView(
                    expand=True,
                    spacing=1,
                    controls=(
                        [_item_row(it) for it in items]
                        if items
                        else [
                            ft.Text(
                                "GPV データ取得を開始すると、ここに 1 frame ごとの"
                                "状態 (pending → downloading → done) と "
                                "サイズ・所要時間が表示されます。",
                                size=11, color=ft.Colors.GREY, italic=True,
                            ),
                        ]
                    ),
                ),
            ],
        ),
    )

    # ── Tab body: ターミナル (placeholder) ──
    # ERA5 や CDS API のような「base time + step」モデルに合わない
    # データソースでは、ユーザがまず「データセット定義」を書く必要
    # がある (変数、期間、緯経度範囲、気圧面、aggregation など)。
    # この定義を経て初めて GPV / チャート描画に進める。Python REPL が
    # その自然な場所になるのでターミナルタブで提供する想定。
    terminal_tab_body = ft.Container(
        padding=ft.Padding.all(12),
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Text(
                    "ターミナル / Terminal  (Coming soon)",
                    size=13, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "ERA5 や Copernicus CDS のように「データセットを"
                    "自分で定義する」必要がある場合に使う Python REPL。"
                    "HRES のような「base time + step」モデルでは不要だが、"
                    "再解析の解析ワークフローは標準化できないのでここで"
                    "ad-hoc に組み立てる。",
                    size=12,
                ),
                ft.Container(
                    bgcolor=ft.Colors.BLACK,
                    padding=ft.Padding.all(12),
                    border_radius=4,
                    content=ft.Text(
                        "$ # 1) データセット定義 (ERA5 zarr を遅延ロード)\n"
                        "$ era5 = open_era5(\n"
                        "      vars=['t2m','msl'],\n"
                        "      time=slice('2024-01','2024-12'),\n"
                        "      bbox=[120, 150, 22, 50],  # Japan 周辺\n"
                        "      level=None,\n"
                        "  )\n"
                        "$ # 2) 解析 (1991-2020 平年偏差)\n"
                        "$ anom = era5.t2m - climo_t2m_dec\n"
                        "$ # 3) チャート main area に表示\n"
                        "$ render(anom.mean('time'), region=JAPAN, cmap='RdBu_r')",
                        color=ft.Colors.GREEN_300,
                        size=11,
                        font_family="monospace",
                    ),
                ),
                ft.Text(
                    "実装はまだ。ERA5 接続 + 評価環境 (xarray, regions, "
                    "render helpers をプリロード) と一緒に着手予定。",
                    size=10, color=ft.Colors.GREY, italic=True,
                ),
            ],
        ),
    )

    # ── Tab body: ログ (placeholder) ──
    log_tab_body = ft.Container(
        padding=ft.Padding.all(12),
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Text(
                    "ログ / Logs  (Coming soon)",
                    size=13, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "logger.* 出力の末尾 N 行をここに表示する予定。"
                    "現状はターミナル (stderr) を見る必要があります。",
                    size=12, color=ft.Colors.GREY,
                ),
            ],
        ),
    )

    tab_bodies = [fetch_tab_body, terminal_tab_body, log_tab_body]

    bottom_panel = ft.Column(
        spacing=0,
        controls=[
            bottom_panel_header,
            ft.Container(
                visible=bottom_panel_open,
                height=280,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                content=tab_bodies[bottom_panel_tab],
            ),
        ],
    )

    nav = ft.NavigationBar(
        selected_index={"map": 0, "radar": 1, "amedas": 2}[active_view],
        on_change=lambda e: set_active_view(
            ["map", "radar", "amedas"][e.control.selected_index],
        ),
        destinations=[
            ft.NavigationBarDestination(
                icon=ft.Icons.PUBLIC, label="モデル / Models",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.RADAR, label="ナウキャスト / Nowcast",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.LOCATION_ON, label="地点 / Points",
            ),
        ],
    )

    return ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.Container(content=body, expand=True),
                bottom_panel,
                nav,
            ],
        ),
    )


@ft.component
def ConfigStatusPanel(result: LoadResult):
    if result.status == "created":
        title = "Config file created"
        title_color = ft.Colors.AMBER
        lines = [
            f"A template was written to:\n  {result.path}",
            'Edit it to choose data sources ("forecast_source", '
            '"historical_source", "point_source"), then restart the app.',
            'JMA radar and AMeDAS work even with every source set to "none".',
        ]
    else:  # "invalid"
        title = "Config file is invalid"
        title_color = ft.Colors.RED
        lines = [
            f"Path:\n  {result.path}",
            f"Reason:\n  {result.error}",
            "Fix the file and restart the app.",
        ]

    return ft.Container(
        padding=24,
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Text(title, size=22, weight=ft.FontWeight.BOLD, color=title_color),
                *[ft.Text(line, size=13) for line in lines],
            ],
        ),
    )
