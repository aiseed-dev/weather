# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
"""Effects that start work must stop it.

Flet 1.0.4's ``use_effect`` resets the hook's cleanup from its ``cleanup``
argument on every render, so a cleanup returned by the setup never runs: a
play loop kept stepping after 止める, and every restart added a loop.
"""

import ast
import asyncio
from pathlib import Path

import flet as ft

from aiseed_weather.components import task_effect

COMPONENTS = Path(__file__).resolve().parents[1] / "src" / "aiseed_weather" / "components"


def _returns_value(fn: ast.FunctionDef) -> bool:
    """A ``return <something>`` in the function itself (nested defs excluded)."""
    todo = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            return True
        todo.extend(ast.iter_child_nodes(node))
    return False


def test_no_effect_returns_its_cleanup():
    problems = []
    for path in sorted(COMPONENTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "use_effect"):
                continue
            has_cleanup = len(call.args) >= 3 or any(k.arg == "cleanup" for k in call.keywords)
            setup = call.args[0] if call.args else None
            if (not has_cleanup and isinstance(setup, ast.Name) and setup.id in defs
                    and _returns_value(defs[setup.id])):
                problems.append(f"{path.name}:{call.lineno}: {setup.id} returns its cleanup")
    assert not problems, "\n".join(problems)


def test_use_task_effect_cancels_through_the_cleanup_argument(monkeypatch):
    calls = {}

    class Ref:
        current = None

    monkeypatch.setattr(ft, "use_ref", lambda initial=None: Ref())
    monkeypatch.setattr(ft, "use_effect",
                        lambda setup, deps, cleanup: calls.update(setup=setup, deps=deps,
                                                                  cleanup=cleanup))

    async def run():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(3600)

        task_effect.use_task_effect(work, ["a"])
        assert calls["deps"] == ["a"] and callable(calls["cleanup"])
        calls["setup"]()
        await started.wait()
        task = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()][0]
        calls["cleanup"]()
        await asyncio.sleep(0)
        return task.cancelled()

    assert asyncio.run(run())
    # A setup that returns None starts nothing.
    task_effect.use_task_effect(lambda: None, [])
    calls["setup"]()
    calls["cleanup"]()
