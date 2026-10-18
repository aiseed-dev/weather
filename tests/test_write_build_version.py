# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "write_build_version", _REPO / "tools" / "write_build_version.py",
)
wbv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wbv)


def test_a_tag_gives_its_number():
    assert wbv.version_for("refs/tags/app-v0.1.1", "abcdef1234") == ("0.1.1", True)
    assert wbv.version_for("refs/tags/app-v12.0.30", "abcdef1234") == ("12.0.30", True)


@pytest.mark.parametrize("name", ["app-v0.1", "app-v1.2.3.4", "app-v1.2.x", "v0.1.1", "app-v0.1.1-rc1", "app-v"])
def test_a_tag_of_the_wrong_shape_is_refused(name):
    with pytest.raises(ValueError, match="app-vX.Y.Z"):
        wbv.version_for(f"refs/tags/{name}", "abcdef1234")


def test_a_branch_gets_a_dev_version_with_the_short_sha():
    assert wbv.version_for("refs/heads/main", "0123456789abcdef") == ("0.0.0.dev0+0123456", False)
    assert wbv.version_for("", "0123456789abcdef") == ("0.0.0.dev0+0123456", False)


def _tree(tmp_path):
    (tmp_path / "src" / "aiseed_weather").mkdir(parents=True)
    return tmp_path


def test_main_writes_the_file_and_exports_the_release_version(tmp_path):
    root = _tree(tmp_path)
    github_env = tmp_path / "github_env"
    env = {"GITHUB_REF": "refs/tags/app-v0.2.0", "GITHUB_SHA": "abc", "GITHUB_ENV": str(github_env)}
    assert wbv.main(env, root) == 0
    assert (root / "src" / "aiseed_weather" / "_build_version.py").read_text() == '__version__ = "0.2.0"\n'
    assert github_env.read_text() == "BUILD_VERSION=0.2.0\n"


def test_main_for_a_branch_exports_nothing(tmp_path):
    root = _tree(tmp_path)
    github_env = tmp_path / "github_env"
    env = {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "abcdef123456", "GITHUB_ENV": str(github_env)}
    assert wbv.main(env, root) == 0
    assert "0.0.0.dev0+abcdef1" in (root / "src" / "aiseed_weather" / "_build_version.py").read_text()
    assert not github_env.exists()


def test_main_fails_on_a_bad_tag_and_writes_nothing(tmp_path, capsys):
    root = _tree(tmp_path)
    env = {"GITHUB_REF": "refs/tags/app-v1.2", "GITHUB_SHA": "abc"}
    assert wbv.main(env, root) == 1
    assert "app-vX.Y.Z" in capsys.readouterr().err
    assert not (root / "src" / "aiseed_weather" / "_build_version.py").exists()


def test_init_reads_the_written_build_version(monkeypatch):
    import types

    import aiseed_weather

    fake = types.ModuleType("aiseed_weather._build_version")
    fake.__version__ = "9.8.7"
    monkeypatch.setitem(sys.modules, "aiseed_weather._build_version", fake)
    try:
        assert importlib.reload(aiseed_weather).__version__ == "9.8.7"
    finally:
        monkeypatch.delitem(sys.modules, "aiseed_weather._build_version")
        importlib.reload(aiseed_weather)


def test_init_falls_back_without_a_build_version(monkeypatch):
    import aiseed_weather

    # None in sys.modules makes the import raise ImportError, as a missing file does.
    monkeypatch.setitem(sys.modules, "aiseed_weather._build_version", None)
    try:
        assert importlib.reload(aiseed_weather).__version__ == "0.0.0.dev0"
    finally:
        monkeypatch.delitem(sys.modules, "aiseed_weather._build_version")
        importlib.reload(aiseed_weather)
