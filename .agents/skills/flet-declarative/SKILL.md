---
name: flet-declarative
description: How to write Flet UI code in this project. Components mode only, no imperative page.update().
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

## Async event handlers

```python
async def handle_layer_change(e):
    # service call; Flet handles re-render after state mutates
    data = await forecast_service.fetch(layer)
    forecast_state.current_data = data
```

Never call `page.update()` from these handlers.

## Forbidden

- `UserControl` subclass (old API, removed)
- Explicit `page.update()` in event handlers
- Storing `ft.Control` objects as fields or in state
- `page.add(...)` outside `main(page)` setup
- Mixing imperative and declarative styles in the same file

## When uncertain

If a Flet API seems to require imperative style, treat it as a sign to redesign
the component boundary rather than reach for `page.update()`. Ask before writing
imperative code.
