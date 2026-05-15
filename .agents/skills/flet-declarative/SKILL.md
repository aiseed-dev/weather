---
name: flet-declarative
description: How to write Flet UI code in this project. Components mode only, no imperative page.update(). Targets Flet 0.85+ APIs (ft.Router, ft.use_dialog).
---

## Core rule

UI is **derived from state**. Mutate state, let Flet re-render. Never write
imperative chains like `control.value = x; page.update()`.

## Required patterns

### Components

```python
import flet as ft

@ft.component
def MapView(layer: str, on_layer_change):
    return ft.Column(
        controls=[
            LayerSelector(current=layer, on_change=on_layer_change),
            MapImage(layer=layer),
        ]
    )
```

### Local UI state

```python
@ft.component
def LayerSelector(current, on_change):
    hovered, set_hovered = ft.use_state(False)
    ...
```

### Shared data model (only when truly shared)

```python
@ft.observable
class ForecastState:
    run_time: datetime
    selected_layer: str
    available_layers: list[str]
```

Pass the model into components; components mutate fields directly.
Flet re-renders observers automatically.

### Entry point

```python
def main(page: ft.Page):
    page.title = "AIseed Weather"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.render(App)

if __name__ == "__main__":
    ft.run(main)
```

### Top-level wrapping

The component returned at root must wrap content in `ft.SafeArea`:

```python
@ft.component
def App():
    return ft.SafeArea(content=MapView(...))
```

## Navigation: ft.Router (Flet 0.85+)

Use `ft.Router` for multi-view apps. Do NOT roll your own
`page.route`-listener / `page.views.append` logic — `Router` is the
declarative replacement.

```python
@ft.component
def App():
    return ft.Router(
        routes=[
            ft.Route(component=AppShell, outlet=True, children=[
                ft.Route(index=True, component=MapView),
                ft.Route(path="radar", component=RadarView),
                ft.Route(path="amedas", component=AmedasView),
            ]),
        ],
    )
```

Parent routes that wrap children use `outlet=True` + render
`ft.use_route_outlet()` somewhere in their tree. The matched child is
inserted there.

`ft.Route(component=...)` invokes `component` with no arguments. To
inject props (e.g. `settings`), wrap it in a closure inside the parent
component so the closure captures the props from `use_state`:

```python
def render_map():
    return MapView(settings=settings, fetch_session=fetch_session)

ft.Route(index=True, component=render_map)
```

Navigation uses `page.navigate("/path")` from event handlers — never
`page.go(...)` (older API) or mutating `page.route`. The current path is
read inside components via `ft.use_route_location()`, e.g. to highlight
the active tab in a NavigationBar:

```python
location = ft.use_route_location()
selected_idx = next(
    (i for i, p in enumerate(_NAV_PATHS) if location == p), 0,
)
```

## Dialogs: ft.use_dialog (Flet 0.85+)

Dialogs are reactive state. Do NOT call `page.show_dialog(...)` or
`page.close_dialog()` — those are the imperative API and don't fit
`@ft.component`. Use the `ft.use_dialog` hook:

```python
@ft.component
def DeleteButton():
    show, set_show = ft.use_state(False)

    ft.use_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete report.pdf?"),
            content=ft.Text("This cannot be undone."),
            actions=[
                ft.Button("Delete", on_click=lambda _: set_show(False)),
                ft.TextButton("Cancel", on_click=lambda _: set_show(False)),
            ],
            on_dismiss=lambda _: set_show(False),
        )
        if show else None,
    )

    return ft.Button("Delete File", on_click=lambda _: set_show(True))
```

Rules:

- One `use_dialog(...)` call per logical dialog. Use multiple hooks in
  the same component to manage independent dialogs (catalog, region,
  time, fetch-confirm, ...). The hook tracks each call site separately.
- The argument is the dialog *instance* when shown, or `None` when
  hidden. The hook diffs frozen fields between renders, so a `TextField`
  inside the dialog keeps cursor and selection across re-renders even
  though Python hands the framework a new control object every render.
- Dismiss handler should `set_show(False)` so the next render passes
  `None` and the hook cleanly removes the dialog.

## Async event handlers

```python
async def handle_layer_change(e):
    # service call; Flet handles re-render after state mutates
    data = await forecast_service.fetch(layer)
    forecast_state.current_data = data
```

Never call `page.update()` from these handlers.

Scheduling async work from sync handlers uses
`ft.context.page.run_task(coro_fn, *args, **kwargs)` — there is no
top-level `ft.run_task` and never was. `run_task` requires a coroutine
*function*, not a lambda — pass `load` directly, not `lambda: load(...)`.

## Shared reactive state: @ft.observable

For state that lives across components — App-level fetch lifecycle,
shared selectors, anything more than one component reads — use
`@ft.observable`. The decorator works on a `@dataclass` (in either
order); place the instance in `ft.use_state` so the auto-subscription
machinery hooks the host component up:

```python
from dataclasses import dataclass, field
import flet as ft

@ft.observable
@dataclass
class FetchSession:
    running: bool = False
    progress: dict = field(default_factory=lambda: {"done": 0, "total": 0})
    items: list = field(default_factory=list)

@ft.component
def App():
    session, _ = ft.use_state(lambda: FetchSession())   # auto-subscribes
    # Pass `session` down as a prop. Children that read its fields
    # auto-subscribe too via their own use_state on the same instance,
    # or via direct attribute access inside their render body.
```

Mutating fields — `session.running = True`, `session.items[:] = [...]`
— notifies every subscribed component, which re-renders. Lists and
dicts are auto-wrapped, so in-place ops (`session.items.append(x)`,
`session.progress["done"] = 5`) also notify.

When to use `@ft.observable` vs `ft.use_state`:

- One component owns the state → `ft.use_state` (local hook).
- State must survive route navigation or be shared between siblings →
  `@ft.observable` model held by a common ancestor.

The setter returned by `ft.use_state(lambda: X())` for an observable
value is essentially unused — you mutate fields, not the instance.

## Non-reactive refs: ft.use_ref

Some state isn't reactive — asyncio.Task references, cancel_events,
holder values that async closures need at invocation time but should
NOT trigger re-renders. Use `ft.use_ref`:

```python
task_ref = ft.use_ref(lambda: {"task": None, "cancel_event": None})

def stop():
    ev = task_ref.current.get("cancel_event")
    if ev is not None:
        ev.set()
    task_ref.current["task"] = None
```

`ft.use_ref` returns a `MutableRef` with `.current` for read/write. The
ref's identity is stable across renders, and writes never re-render the
component.

What MUST NOT go in a ref:

- `ft.Control` instances (controls are derived from state; storing them
  defeats reactivity and breaks the use_dialog frozen-diff machinery)
- Reactive UI state — region, layer, selected cycle, progress, status
  text. Those belong in `ft.use_state` or `@ft.observable`. Storing
  them in a ref hides them from the framework and produces stale UI.

## Forbidden

- `UserControl` subclass (old API, removed)
- Explicit `page.update()` in event handlers
- Storing `ft.Control` objects as fields or in state
- `page.add(...)` outside `main(page)` setup
- `page.show_dialog(...)` / `page.close_dialog()` — use `ft.use_dialog`
- `page.go(...)` / mutating `page.route` — use `page.navigate(...)`
- Rolling your own route listener — use `ft.Router`
- Mixing imperative and declarative styles in the same file
- Direct control mutation: `control.value = x`, `dialog.open = True`,
  `nav.selected_index = i` — always derive from state instead

## When uncertain

If a Flet API seems to require imperative style, treat it as a sign to
redesign the component boundary rather than reach for `page.update()`.
Ask before writing imperative code.
