# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""An effect that runs one asyncio task while its dependencies hold.

Flet 1.0.4's ``use_effect`` sets the hook's cleanup from its ``cleanup``
argument on every render, so a cleanup *returned* by the setup is dropped
at the next render and never runs. A task started that way outlives a
dependency change and the component itself: a loader for the old layer
keeps adding frames, a play loop keeps stepping after 止める, and each
restart adds another loop. Passing the cleanup as the argument, with the
task in a ref, is what Flet does call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

import flet as ft


def use_task_effect(start: Callable[[], Coroutine[Any, Any, Any] | None],
                    dependencies: Sequence[Any]) -> None:
    """Run the coroutine ``start()`` returns; cancel it when a dependency
    changes or the component unmounts. ``start`` may return None (no task)."""
    task_ref = ft.use_ref(None)

    def setup():
        coroutine = start()
        if coroutine is not None:
            task_ref.current = asyncio.get_running_loop().create_task(coroutine)

    def cleanup():
        task, task_ref.current = task_ref.current, None
        if task is not None:
            task.cancel()

    ft.use_effect(setup, dependencies, cleanup)
