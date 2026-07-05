"""季節判定 (Jma.IsSummer / Jma.IsSeason) の Python 移植。

元は DateTime.Now を使っていたが、静的生成では「生成時刻」を基準に判断し、
結果を JSON / テンプレートコンテキストに焼き込む。
"""
from __future__ import annotations

from datetime import datetime


def is_summer(dt: datetime) -> bool:
    """夏用の表示にするか（Jma.IsSummer 相当）。"""
    m, d = dt.month, dt.day
    if m in (1, 2):
        return False
    if m == 3:
        return d >= 18
    if m in (4, 5, 6, 7, 8):
        return True
    if m == 9:
        return d < 21
    return False  # 10, 11, 12


def is_season(dt: datetime) -> int:
    """1=夏 / 2=冬（Jma.IsSeason 相当）。"""
    m, d = dt.month, dt.day
    if 5 < m < 10:
        return 1
    if m == 5:
        return 2 if d < 16 else 1
    if m == 10:
        return 1 if d < 16 else 2
    return 2
