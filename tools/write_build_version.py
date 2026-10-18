# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Write the app's version for a build: the git tag is the only source.

CI (.github/workflows/windows-app.yml) runs this before building. For a tag
``app-vX.Y.Z`` it writes ``X.Y.Z`` into the git-ignored
src/aiseed_weather/_build_version.py and exports ``BUILD_VERSION`` so the
step that builds can pass ``--build-version``. For anything else (a push to
main, a manual run) it writes ``0.0.0.dev0+<short sha>`` and exports nothing.
Standard library only, so it runs before anything is installed.
flet-tools の雛形 recipes/flet-windows-release のもとになった物（https://github.com/aiseed-dev/flet-tools/tree/main/recipes/flet-windows-release）

    GITHUB_REF=refs/tags/app-v0.1.1 GITHUB_SHA=... python tools/write_build_version.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TAG_REF = "refs/tags/"
_TAG = re.compile(r"app-v(\d+\.\d+\.\d+)")


def version_for(ref: str, sha: str) -> tuple[str, bool]:
    """(version, is_release). Raises ValueError for a tag that is not app-vX.Y.Z."""
    if ref.startswith(_TAG_REF):
        name = ref[len(_TAG_REF):]
        match = _TAG.fullmatch(name)
        if not match:
            raise ValueError(f"タグ {name} は app-vX.Y.Z（X・Y・Z は数字）の形ではありません")
        return match.group(1), True
    return f"0.0.0.dev0+{sha[:7]}", False


def write_build_version(version: str, root: Path = ROOT) -> Path:
    path = root / "src" / "aiseed_weather" / "_build_version.py"
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return path


def main(env: dict[str, str] | None = None, root: Path = ROOT) -> int:
    env = os.environ if env is None else env
    try:
        version, is_release = version_for(env.get("GITHUB_REF", ""), env.get("GITHUB_SHA", "0000000"))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    path = write_build_version(version, root)
    print(f"{path.relative_to(root)} に版 {version} を書きました")
    github_env = env.get("GITHUB_ENV")
    if is_release and github_env:
        with open(github_env, "a", encoding="utf-8") as f:
            f.write(f"BUILD_VERSION={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
