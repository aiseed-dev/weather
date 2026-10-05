# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Font setup shared by every matplotlib figure in the app."""

from __future__ import annotations

import logging

import matplotlib
import matplotlib.font_manager as fm

logger = logging.getLogger(__name__)


def configure_cjk_font() -> None:
    """Pin matplotlib's default font to one that has CJK glyphs.

    matplotlib ships with DejaVu Sans, which has no CJK support, so
    every Japanese label in this chart (タイトル, 凡例, 軸ラベル,
    '現在' marker) renders as tofu (□□□). Walk a list of common
    Japanese-capable fonts and pick the first one that's actually
    registered; fall back to a substring scan if none of the named
    ones is present. Log which font won so debugging a wrong-font
    output is one ``grep`` away.
    """
    candidates = (
        # Linux — Noto CJK is the most common modern packaging.
        "Noto Sans CJK JP",
        "Noto Sans JP",
        # Linux — older / IPA-based bundles still very common on
        # Japanese-locale Debian/Ubuntu.
        "IPAexGothic",
        "IPAGothic",
        "IPAPGothic",
        "TakaoPGothic",
        "TakaoGothic",
        "VL PGothic",
        # macOS.
        "Hiragino Sans",
        "Hiragino Maru Gothic Pro",
        # Windows.
        "Yu Gothic",
        "Meiryo",
        "MS Gothic",
    )
    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            matplotlib.rcParams["font.family"] = name
            matplotlib.rcParams["axes.unicode_minus"] = False
            logger.debug("Using CJK font: %s", name)
            return
    # Substring fallback for distro-specific font naming we didn't
    # think of.
    for f in fm.fontManager.ttflist:
        n = f.name.lower()
        if any(t in n for t in ("cjk", "japan", "gothic", "ipa")):
            matplotlib.rcParams["font.family"] = f.name
            matplotlib.rcParams["axes.unicode_minus"] = False
            logger.info(
                "Using CJK font via substring match: %s (%s)",
                f.name, f.fname,
            )
            return
    logger.warning(
        "No CJK font found; chart labels with Japanese characters "
        "will render as tofu (□). Install fonts-noto-cjk or "
        "fonts-ipafont-gothic.",
    )
