---
name: flet-component-basics
description: Build new Flet 0.85+ desktop applications from scratch using declarative @ft.component style with hooks (use_state). Covers entry point, component structure, local state, event handlers, auto-update behavior, and async patterns for CPU-bound or long-running tasks. Use when creating a new Flet app (not converting an existing imperative one). Triggers on mentions of "@ft.component", "ft.use_state", "page.render", "flet run", or starting a new Flet desktop project in Python. For converting an existing imperative Flet app to declarative style, use the imperative-to-declarative-flet skill instead.
---

# Flet Component Basics (Flet 0.85+)

Build a new Flet desktop app from scratch using the declarative `@ft.component` style.

This skill covers the **starting point** — entry, components, local state, events. For dialogs, routing, file picking, or audio, see the corresponding dedicated skills (loaded only when needed).

## Entry point

A declarative Flet app has a root component and is started with `page.render`:

```python
import flet as ft

@ft.component
def App():
    return ft.Text("Hello")

ft.run(lambda page: page.render(App))
```

If you need to configure the page (title, theme, window size) before the app renders, use `before_main`:

```python
def configure(page: ft.Page):
    page.title = "My App"
    page.window.width = 800
    page.window.height = 600

ft.run(lambda page: page.render(App), before_main=configure)
```

Run with `flet run` from the project directory. No `page.add()` calls needed in the App — the component tree is the UI.

## Components

A component is a function decorated with `@ft.component` that returns a control tree:

```python
@ft.component
def Greeting():
    return ft.Text("Hello", size=20)

@ft.component
def App():
    return ft.Column([
        Greeting(),
        ft.Button("Click me"),
    ])
```

Components compose by being called like functions inside other components. They are self-contained: each manages its own state via hooks.

## Local state with use_state

For UI state that lives inside a single component (input text, hover flags, selected item, toggle):

```python
@ft.component
def Counter():
    count, set_count = ft.use_state(0)
    return ft.Row([
        ft.Text(f"Count: {count}"),
        ft.Button("Increment", on_click=lambda: set_count(count + 1)),
    ])
```

`ft.use_state(initial)` returns a `(value, setter)` tuple. Calling the setter triggers a re-render. The setter accepts either a new value or a function that receives the current value:

```python
set_count(count + 1)           # direct value
set_count(lambda c: c + 1)     # functional update (safer for rapid updates)
```

## State design rules

- **Store data, not controls.** Put ids, enums, dataclasses, lists, dicts in state — never live `Control` objects. Build controls during render from the state.
- **Lift state up when shared.** If two sibling components need the same state, hold it in their parent and pass values + setters down.
- **Derive, don't duplicate.** If a value can be computed from existing state, compute it during render instead of storing it.

## Event handlers

Event handlers can omit the event argument when not needed:

```python
button.on_click = lambda: set_count(count + 1)        # no event arg
button.on_click = lambda e: print(e.control.text)     # with event arg
```

Both sync and async handlers work:

```python
async def handle_save():
    set_saving(True)
    await save_to_disk()
    set_saving(False)

button.on_click = handle_save
```

## Auto-update

`Control.update()` is called automatically after an event handler returns. Do not call `page.update()` or `control.update()` manually in declarative components.

For long-running async handlers that need to refresh UI mid-task, use `yield`:

```python
async def handle_solve():
    set_status("Solving...")
    yield                       # UI updates here
    await asyncio.sleep(2)
    set_status("Done")
```

## Page access

Reach the current page from anywhere via `ft.context.page`:

```python
ft.context.page.title = "Updated"
ft.context.page.navigate("/settings")
```

Do not pass `page` through component arguments — use `ft.context.page` instead.

## Async and CPU-bound work

Flet 1.0 uses a single-threaded async UI model. Blocking calls freeze the UI.

- **Never use `time.sleep()`** in event handlers. Use `await asyncio.sleep()`.
- **For CPU-bound work** (heavy computation, image processing, solvers), offload to a thread:

```python
import asyncio

async def handle_solve():
    set_status("Solving...")
    result = await asyncio.to_thread(cpu_heavy_solve, puzzle)
    set_solution(result)
    set_status("Done")
```

`asyncio.to_thread` runs the function in a thread pool and awaits its result without blocking the UI.

## What this skill does not cover

Load the corresponding skill when needed:

- **Dialogs** (alert, confirm, modal): use `ft.use_dialog()`. Separate skill.
- **Routing** (multi-page apps): use `ft.Router`. Separate skill.
- **Services** (FilePicker, Audio, Clipboard): add to `page.services`. Separate skill.
- **Converting an existing imperative app** to declarative: use the `imperative-to-declarative-flet` skill.

## Anti-patterns to avoid

- Calling `page.update()` or `control.update()` manually — auto-update handles it.
- Storing `Control` objects in `use_state` — store data, build controls from it.
- Using `time.sleep()` in event handlers — freezes the UI.
- Running CPU-heavy work directly in an async handler — blocks the event loop. Use `asyncio.to_thread()`.
- Adding external state management libraries (e.g. FletX). Built-in hooks (`use_state`, `use_dialog`) cover the common cases.
- Passing `page` through component arguments — use `ft.context.page`.
