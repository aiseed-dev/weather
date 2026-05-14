---
name: user-action-fetch
description: How and when data is fetched. The app fetches only in response to an explicit user button press; mount-time navigation does not count. Read when implementing any view that needs data.
---

## The rule

Data is fetched **only** in response to an explicit user button press.
The user actions that trigger fetching are:

1. **Pressing 取得 / Fetch** on a view that is sitting idle
2. **Pressing 再取得 / Refresh** on a view that already has data
3. **Changing a parameter** that requires new data (different layer,
   region, or reference period) — implicit re-fetch, treated as a user
   action because the user just clicked something

The app does **not**:

- Fetch on view mount (navigating to a tab does **not** start a fetch)
- Fetch anything at startup
- Run any kind of timer or periodic refresh
- Pre-fetch adjacent data the user might want next
- Refresh in the background when a view is hidden

The user is reading specific data sources because they chose to. Making
them say "yes, fetch this" is a feature, not friction — bandwidth and
publisher politeness both demand it.

## State machine for a data-backed view

```
idle ──[Fetch]──▶ loading ──▶ ready    ──[Refresh]──▶ loading
                          ╲           ╲
                           ╲           ╲──[error]──▶ error
                            ╲                          │
                             ╲──[failure]──────────────┘
                                                       │
                                                       ╰──[Retry]──▶ loading
```

Required UI states:

- **idle**: one-line description of what will be fetched, plus a
  prominent `取得 / Fetch` button. No progress indicator yet.
- **loading**: progress indicator + a one-line status string showing
  which phase is running (see "Progress messages" below).
- **ready**: the data, plus a `再取得 / Refresh` button that forces a
  fresh fetch ignoring cache.
- **error**: the error message and a `再取得 / Retry` button.

Disabled / not-configured is a fifth state for views whose data source
the user opted out of in `config.toml` (e.g. ECMWF map view with
`forecast_source = "none"`). Show why and how to fix it; do not show a
fetch button.

## Progress messages

The terminal already gets per-phase timing via `logger.info`. The UI
needs the same granularity but as plain text the user can read. Update
the `progress` state at each phase boundary so the user always knows
which step is running:

| Phase | Suggested message |
|-------|-------------------|
| Service construction | "Initializing forecast service…" |
| Latest-run probe | "Probing latest ECMWF run…" |
| Download / decode | "Fetching MSL grid · 20260514 00z…" |
| Render | "Rendering chart (matplotlib + cartopy)…" |
| Encode | "Encoding PNG…" |

Wording can vary by view; keep them short and in present-continuous
tense.

## Cache window per source

When the user presses Fetch, cached data is reused if it falls inside
the source's update cadence:

| Source | Cache window |
|--------|--------------|
| JMA radar | 10 min |
| JMA AMeDAS | 10 min |
| ECMWF forecast (same run) | until next run publication |
| ERA5 fields | indefinite (immutable for past dates) |
| ERA5 climatology aggregations | indefinite (immutable) |
| Open-Meteo forecast | 1 hour |

The view never inspects cache freshness — it just calls `fetch()`. The
service decides based on the data source's update cadence.

The Refresh button passes `force=True` to bypass cache.

## Implementation pattern in Flet

```python
@ft.component
def MapView(settings):
    state, set_state = ft.use_state("idle")        # idle | loading | ready | error
    data, set_data = ft.use_state(None)
    error, set_error = ft.use_state(None)
    progress, set_progress = ft.use_state("")

    async def load(force: bool = False):
        set_state("loading")
        set_error(None)
        set_progress("Probing…")
        try:
            run = await service.latest_run(...)
            set_progress(f"Fetching · {run}…")
            d = await service.fetch(..., force=force)
            set_progress("Rendering…")
            fig = await asyncio.to_thread(render, d)
            set_data(fig_bytes)
            set_progress("")
            set_state("ready")
        except Exception as e:
            logger.exception("load failed")
            set_error(str(e))
            set_state("error")

    if state == "idle":
        return ft.Column([
            ft.Text("MSL · ECMWF IFS, latest available run."),
            ft.FilledButton(
                content=ft.Text("取得 / Fetch"),
                on_click=lambda _: ft.context.page.run_task(load),
            ),
        ])
    # ... loading / ready / error branches
```

Two important details:

- **No `ft.use_effect`** for mount-time fetching. The idle state is the
  view's resting state until the user acts.
- `ft.context.page.run_task(load, force=True)` is how a sync `on_click`
  schedules an async coroutine in Flet 0.85. Pass the coroutine function
  and its args separately — wrapping in `lambda: load(...)` will be
  rejected because the lambda is not a coroutine function.

## What the service must support

Every service used in this pattern exposes a `force: bool` parameter on
its fetch method:

```python
class RadarService:
    async def fetch(self, *, force: bool = False) -> RadarSnapshot:
        if not force and self._cache_is_fresh():
            return self._load_from_cache()
        return await self._download_and_cache()
```

## When user changes a parameter

If the user changes something that requires new data (e.g. selects a
different ECMWF layer or step):

- This is a user action → fetch is allowed without a button press
- The view goes back to `loading` state
- The same `load` function runs with the new parameters

This is the one case where a fetch is triggered without an explicit
button press, because the parameter widget itself was the user's
action.

## What goes in the timestamp display

Every data-backed view in the `ready` state shows, prominently:

- **The data's valid time** in both UTC and local (JST for the user's
  locale) where applicable
- **When it was fetched** ("fetched 3 min ago" is fine)
- Whether it came from cache is implementation detail; don't expose
  unless it helps the user decide whether to refresh

Bad: "Last updated 12:30" (ambiguous — last fetched? last observed?)
Good: "Valid 12:30 JST · fetched 12:33 JST"

## Hidden views

When the user navigates away from a view, the view's data stays in
cache on disk. When they come back:

- The view mounts again in `idle` state
- The user has to press Fetch to load (one extra click, on purpose)
- The fetch finds the cached data within the freshness window → instant

The user never pays a network cost for re-entering a screen, but they
also never have a screen silently re-fetch on focus.

## Forbidden

- `ft.use_effect` to auto-fetch on mount
- Any `setInterval`, `Timer`, or periodic refresh logic
- Fetching during app startup before any view is shown
- Fetching adjacent data "in case the user wants it"
- Hidden refresh on focus / window restoration
- Replacing a displayed value with a fresh one without the user asking
- Auto-retry loops on failure (one attempt, then show error)
- A progress indicator without a status message (the user should always
  know which phase is running)
