# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

from dataclasses import dataclass, field
from typing import Callable

import flet as ft

from aiseed_weather.components.amedas_view import AmedasView
from aiseed_weather.components.map_view import MapView
from aiseed_weather.components.radar_view import RadarView
from aiseed_weather.models import user_settings
from aiseed_weather.models.user_settings import LoadResult


# Path → tab index mapping used by both the NavigationBar (active tab
# highlight) and the Route table. "/" is map by convention because it's
# the highest-traffic view and the natural landing page.
_NAV_PATHS: tuple[str, ...] = ("/", "/radar", "/amedas")


@ft.observable
@dataclass
class FetchSession:
    """Reactive GPV fetch lifecycle state.

    Owned by App (so a running download survives tab navigation) and
    shared with MapView + AppShell as the single source of truth.
    Mutating a field — ``session.running = True`` — notifies every
    component that read it; the @ft.observable decorator hooks into
    ft.use_state's auto-subscription machinery.

    Side-effect plumbing (the asyncio Task and its cancel_event) is
    deliberately NOT here: it isn't UI state and shouldn't trigger
    re-renders. It lives in a separate ft.use_ref next to the session.
    """

    running: bool = False
    # Per-frame fetch items: pip install-style detailed view (status
    # icon + label + size + duration). Each item is a dict:
    #   {"step": int, "param": str, "stitched": bool,
    #    "status": "pending"|"checking"|"downloading"|"done"|"cached"
    #              |"failed"|"cancelled",
    #    "size_bytes": int|None, "duration_s": float|None}
    items: list = field(default_factory=list)
    progress: dict = field(
        default_factory=lambda: {"done": 0, "total": 0},
    )
    status_text: str = ""


@dataclass(frozen=True)
class FetchController:
    """Bundle the reactive session + non-reactive task ref + stop fn.

    Threaded through props instead of three separate args. Inert wrapper
    — only the ``session`` field is observable; ``task_ref`` is a plain
    MutableRef and ``stop`` is a closure.
    """

    session: FetchSession
    # ft.MutableRef is the return type of ft.use_ref but isn't re-
    # exported at the top level in Flet 0.85. ``Any`` keeps the
    # annotation honest without leaking the internal import path.
    task_ref: object
    stop: Callable[[], None]


@ft.component
def App():
    # Config is read once at startup. Editing the TOML and restarting is
    # the only way to change sources — see the `first-run-setup` skill.
    result, _ = ft.use_state(user_settings.load_or_init())

    # ──────────────────────────────────────────────────────────
    # App-level GPV fetch session.
    #
    # Owned here (not in MapView) so the running download survives
    # tab navigation. MapView kicks off fetches but the lifecycle
    # lives at App scope; the bottom panel + Stop button stay
    # reachable from any tab.
    # ──────────────────────────────────────────────────────────
    # use_state on an Observable auto-attaches the component to the
    # observable's subscription list, so field mutations trigger a
    # re-render here. The setter is unused — we never wholesale-replace
    # the session, only mutate its fields.
    session, _ = ft.use_state(lambda: FetchSession())
    # Task + cancel_event are not reactive: re-rendering on a task
    # handle swap would be noise. ft.use_ref is the proper
    # non-reactive holder.
    task_ref = ft.use_ref(lambda: {"task": None, "cancel_event": None})

    def stop_fetch():
        ev = task_ref.current.get("cancel_event")
        if ev is not None:
            ev.set()
        task = task_ref.current.get("task")
        if task is not None and not task.done():
            task.cancel()
        task_ref.current["task"] = None
        task_ref.current["cancel_event"] = None
        session.running = False
        session.status_text = ""

    controller = FetchController(
        session=session, task_ref=task_ref, stop=stop_fetch,
    )

    if result.status != "ok":
        return ft.SafeArea(
            expand=True, content=ConfigStatusPanel(result=result),
        )

    settings = result.settings

    # Route component wrappers. ft.Route invokes `component` with no
    # arguments, so a closure is the canonical way to inject settings
    # and the shared fetch controller.
    def render_shell():
        return AppShell(settings=settings, fetch=controller)

    def render_map():
        return MapView(settings=settings, fetch=controller)

    def render_radar():
        return RadarView(settings=settings)

    def render_amedas():
        return AmedasView(settings=settings)

    return ft.Router(
        routes=[
            ft.Route(
                component=render_shell,
                outlet=True,
                children=[
                    ft.Route(index=True, component=render_map),
                    ft.Route(path="radar", component=render_radar),
                    ft.Route(path="amedas", component=render_amedas),
                ],
            ),
        ],
    )


@ft.component
def AppShell(settings, fetch: FetchController):
    """Parent-route layout: outlet body + bottom panel + nav.

    Renders the matched child route into the central area. Reads
    ``use_route_location()`` to keep the NavigationBar highlight in
    sync with the URL, so a deep-link or back-button navigation
    correctly updates the tab indicator.
    """
    outlet = ft.use_route_outlet()
    location = ft.use_route_location()

    session = fetch.session
    fetch_running = session.running
    fetch_progress = session.progress
    fetch_status_text = session.status_text
    fetch_items = session.items
    stop_fetch = fetch.stop

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

    items = fetch_items
    n_done = sum(1 for it in items if it["status"] in ("done", "cached"))
    n_failed = sum(1 for it in items if it["status"] == "failed")
    n_total = len(items)
    total_bytes = sum(
        (it["size_bytes"] or 0) for it in items
        if it["status"] in ("done", "cached")
    )
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
                ft.Text(
                    "重要: ERA5 アーカイブ全体は ~10 TB 規模。"
                    "「データセット定義」と「計算実行」を分離し、"
                    "実行前に必ず ① 取得・処理されるバイト数の見積もり、"
                    "② 確認ダイアログ、③ キャンセル可能なバックグラウンド"
                    "実行 の 3 段構えにする。pip install 風の per-chunk"
                    "進捗もそのまま再利用する想定。",
                    size=11, color=ft.Colors.AMBER_900,
                ),
                ft.Container(
                    bgcolor=ft.Colors.BLACK,
                    padding=ft.Padding.all(12),
                    border_radius=4,
                    content=ft.Text(
                        "$ # 1) データセット定義 (lazy; この時点では 0 byte)\n"
                        "$ era5 = open_era5(\n"
                        "      vars=['t2m','msl'],\n"
                        "      time=slice('2024-01','2024-12'),\n"
                        "      bbox=[120, 150, 22, 50],  # Japan 周辺\n"
                        "      level=None,\n"
                        "  )\n"
                        "  → 推定 1.2 GB · 8760 time steps · 4 chunks\n"
                        "\n"
                        "$ # 2) 解析定義 (まだ実行されない)\n"
                        "$ anom = era5.t2m - climo_t2m_dec\n"
                        "\n"
                        "$ # 3) 実行 (確認ダイアログ → bg 取得 → 描画)\n"
                        "$ render(anom.mean('time'), region=JAPAN, cmap='RdBu_r')\n"
                        "  → 取得が必要: 1.2 GB / 推定 4 分  [実行] [キャンセル]",
                        color=ft.Colors.GREEN_300,
                        size=11,
                        font_family="monospace",
                    ),
                ),
                ft.Text(
                    "実装はまだ。ERA5 接続 + 評価環境 (xarray, regions, "
                    "render helpers をプリロード) + サイズ見積もり層 と"
                    "一緒に着手予定。",
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

    # NavigationBar mirrors the current route. Clicking a destination
    # calls page.navigate(...) instead of mutating component state, so
    # browser back/forward and deep links Just Work.
    selected_idx = next(
        (i for i, p in enumerate(_NAV_PATHS) if location == p),
        0,
    )
    nav = ft.NavigationBar(
        selected_index=selected_idx,
        on_change=lambda e: ft.context.page.navigate(
            _NAV_PATHS[e.control.selected_index],
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
                ft.Container(content=outlet, expand=True),
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
