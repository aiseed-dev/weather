---
name: user-action-fetch
description: How and when data is fetched. The app fetches only in response to user actions (typically opening a view); never on a timer, never on startup, never in background. Read when implementing any view that needs data.
---

## The rule

Data is fetched **only** in response to a user action. The user actions that
trigger fetching are:

1. **Navigating to a view that needs data** (the primary pattern)
2. **Pressing an explicit "Refresh" button** in that view
3. **Changing a parameter** that requires new data (different layer, different
   region, different reference period)

The app does **not**:
- Fetch anything at startup
- Run any kind of timer or periodic refresh
- Pre-fetch adjacent data the user might want next
- Refresh in the background when a view is hidden

If the user is staring at a 30-minute-old radar image, they see a 30-minute-old
radar image. Showing them stale data without their consent is worse than
making them press a button to update it.

## Fetch-on-open with cache

The standard pattern for any data-backed view:

```
On view mount:
  if cached data is fresh enough (within source's update cadence):
    show cached data
    show timestamp prominently
  else:
    show progress indicator
    fetch
    cache
    show data with timestamp
```

"Fresh enough" is defined per data source:

| Source | Cache window |
|--------|--------------|
| JMA radar | 10 min |
| JMA AMeDAS | 10 min |
| ECMWF forecast (same run) | until next run publication |
| ERA5 fields | indefinite (immutable for past dates) |
| ERA5 climatology aggregations | indefinite (immutable) |
| Open-Meteo forecast | 1 hour |

If a cache hit is used, the view should still display the data's timestamp
("Radar valid 12:30 JST"). The user must always know what time the data is from.

## Manual refresh

Every data-backed view has a "再取得 / Refresh" button. It:

- Forces a fetch ignoring the cache
- Shows a progress indicator on the same view
- Updates the timestamp on success
- Shows an error inline on failure (no toasts that disappear)

The Refresh button is part of the view's controls, not in a global toolbar.
It belongs to the data on this screen.

## Implementation pattern in Flet

```python
@ft.component
def RadarView():
    # state: 'idle' | 'loading' | 'ready' | 'error'
    state, set_state = ft.use_state("idle")
    data, set_data = ft.use_state(None)
    error, set_error = ft.use_state(None)

    async def load(force: bool = False):
        set_state("loading")
        try:
            d = await radar_service.fetch(force=force)
            set_data(d)
            set_state("ready")
        except Exception as e:
            set_error(str(e))
            set_state("error")

    # Mount-time fetch: schedule once when the component first renders.
    ft.use_effect(lambda: ft.context.page.run_task(load), deps=[])

    if state == "loading":
        return ft.ProgressRing()
    if state == "error":
        return ErrorPanel(error, on_retry=lambda _: ft.context.page.run_task(load, force=True))
    return RadarCanvas(data, on_refresh=lambda _: ft.context.page.run_task(load, force=True))
```

Three important details:

- `ft.use_effect(..., deps=[])` runs once per component lifecycle, not on
  every render. This is the "mount" trigger.
- The Refresh button calls `load(force=True)`, which the service interprets
  as "bypass cache".
- Use `ft.context.page.run_task(handler, *args, **kwargs)` to schedule an
  async coroutine from a sync callback. `run_task` requires a coroutine
  *function* — pass `load` directly and let it forward the args; do **not**
  wrap it in `lambda: load(...)` (a lambda is not a coroutine function and
  `run_task` will reject it). There is no top-level `ft.run_task` in
  Flet ≥ 0.85.

## What the service must support

Every service used in this pattern exposes a `force: bool` parameter on its
fetch method:

```python
class RadarService:
    async def fetch(self, *, force: bool = False) -> RadarSnapshot:
        if not force and self._cache_is_fresh():
            return self._load_from_cache()
        return await self._download_and_cache()
```

The service decides what "fresh" means based on the data source's update
cadence. The view does not check timestamps; it just calls fetch.

## When user changes a parameter

If the user changes something that requires new data (e.g. selects a
different ECMWF layer):

- This is a user action → fetch is allowed
- The view goes back to `loading` state
- The same load function runs with the new parameters

This means parameter changes also re-fetch from cache when possible, falling
back to network only when necessary.

## What goes in the timestamp display

Every data-backed view shows, prominently:

- **The data's valid time** in both UTC and local (JST for the user's locale)
- **When it was fetched** (just "fetched 3 min ago" is fine)
- **Whether it came from cache** is implementation detail; don't expose unless
  it helps the user decide whether to refresh

Bad: "Last updated 12:30" (ambiguous — last fetched? last observed?)
Good: "Valid 12:30 JST · fetched 12:33 JST"

## Hidden views

When the user navigates away from a view, the view's data stays in memory
(or in cache on disk). When they come back:

- The view mounts again
- The mount-time fetch logic runs
- It finds cached data within the freshness window
- Display is instant, no fetch happens

This is the desired behavior. The user does not pay a network cost for
re-entering a screen.

## Forbidden

- Any `setInterval`, `Timer`, or periodic refresh logic
- Fetching during app startup before any view is shown
- Fetching adjacent data "in case the user wants it"
- Hidden refresh on focus / window restoration
- Replacing a displayed value with a fresh one without the user asking
- Auto-retry loops on failure (one attempt, then show error)
- Showing "loading…" without progress indication when data is already cached
