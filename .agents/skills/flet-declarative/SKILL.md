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

## Mutable holders for non-reactive state

Some state isn't reactive — asyncio.Task references, cancel_events,
read-back values that async closures need at invocation time (not at
schedule time). Flet doesn't expose a `use_ref` primitive, so the
project pattern is `ft.use_state` with a lazy dict initializer:

```python
task_ref, _ = ft.use_state(lambda: {"task": None, "cancel_event": None})
```

The lazy initializer returns the *same* dict object across renders, so
mutations to it survive. Treat this as the project's idiom for "ref" —
not as imperative state. Tasks and cancel events are side-effect
plumbing, not UI state.

What MUST NOT go in these holders:

- `ft.Control` instances (controls are derived from state; storing them
  defeats reactivity and breaks the use_dialog frozen-diff machinery)
- Anything you'd otherwise re-derive on each render (regions, layers,
  selected cycle — those belong in `use_state` or `@ft.observable`)

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
