# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
"""Every Flet name and keyword argument the app uses exists in the installed Flet.

Flet renames and removes API between releases (``Colors.BLACK26`` became
``Colors.BLACK_26``, ``Page.set_clipboard`` became the ``Clipboard``
service). A missing name inside a component body raises during render,
and Flet's update loop stops on that exception without logging it: the
screen freezes on whatever it showed last. This test finds such names
statically, without starting the UI.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from pathlib import Path

import flet as ft

SRC = Path(__file__).resolve().parents[1] / "src" / "aiseed_weather"


def _aliases(tree: ast.Module) -> dict[str, object]:
    """``import flet as ft`` / ``import flet.canvas as cv`` → {alias: module}."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "flet" or a.name.startswith("flet."):
                    out[a.asname or a.name] = importlib.import_module(a.name)
    return out


def _chain(node: ast.AST) -> list[str] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _page_has(name: str) -> bool:
    return hasattr(ft.Page, name) or name in {f.name for f in dataclasses.fields(ft.Page)}


def _resolve(parts: list[str], roots: dict[str, object]):
    """Returns (object or None, missing dotted name or None)."""
    obj = roots[parts[0]]
    for i, name in enumerate(parts[1:], 1):
        if obj is ft.context and name == "page":
            # The page only exists while the app runs; check the class instead
            # (one level deep: page.window.width etc. are not followed).
            if len(parts) > i + 1 and not _page_has(parts[i + 1]):
                return None, ".".join(parts[: i + 2])
            return None, None
        try:
            obj = getattr(obj, name)
        except AttributeError:
            return None, ".".join(parts[: i + 1])
    return obj, None


def _problems(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = _aliases(tree)
    if not roots:
        return []
    inner = {id(n.value) for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and id(node) not in inner:
            parts = _chain(node)
            if parts and parts[0] in roots:
                _, missing = _resolve(parts, roots)
                if missing:
                    out.append(f"{path.relative_to(SRC)}:{node.lineno}: no {missing}")
        elif isinstance(node, ast.Call):
            parts = _chain(node.func)
            if not parts or parts[0] not in roots:
                continue
            obj, _ = _resolve(parts, roots)
            if obj is None or not callable(obj):
                continue
            try:
                params = inspect.signature(obj).parameters
            except (TypeError, ValueError):
                continue
            if any(p.kind is p.VAR_KEYWORD for p in params.values()):
                continue
            for kw in node.keywords:
                if kw.arg is not None and kw.arg not in params:
                    out.append(f"{path.relative_to(SRC)}:{node.lineno}: "
                               f"{'.'.join(parts)}() has no argument {kw.arg!r}")
    return out


def test_flet_names_and_arguments_exist():
    problems = [p for path in sorted(SRC.rglob("*.py")) for p in _problems(path)]
    assert not problems, f"not in flet {ft.__version__}:\n" + "\n".join(problems)
