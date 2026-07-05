"""SVG チャートのローカル生成（Highcharts の置き換え）。

平年値の雨温図・Home の気温推移グラフをビルド時に純 Python で SVG 化する。
外部ライブラリ依存なし。生成物はテンプレートにインライン埋め込みする。

色は旧サイトの Highcharts 設定を踏襲:
  最高気温 #F92500 / 平均気温 #008000 / 最低気温 #0C00CC / 降水量 #1987E5
"""
from __future__ import annotations

from datetime import date, timedelta

C_TMAX, C_TAVG, C_TMIN, C_PRECIP = "#F92500", "#008000", "#0C00CC", "#1987E5"
FONT = 'font-family="Helvetica Neue, Arial, Hiragino Sans, Meiryo, sans-serif"'


def _smooth_path(pts: list[tuple[float, float]]) -> str:
    """Catmull-Rom 由来の 3 次ベジェで滑らかな折れ線（旧 spline 相当）。"""
    if len(pts) == 1:
        x, y = pts[0]
        return f"M{x:.1f},{y:.1f}"
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def _line_runs(xs, vals):
    """None で分割した (x, v) 連続区間のリスト。"""
    runs, cur = [], []
    for x, v in zip(xs, vals):
        if v is None:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append((x, v))
    if cur:
        runs.append(cur)
    return runs


def uonzu_svg(name: str, monthly: dict, width: int = 720, height: int = 460) -> str:
    """雨温図 SVG。monthly = {tmax/tavg/tmin: [表示値×12], precip: [表示値×12]}"""
    ml, mr, mt, mb = 52, 56, 44, 30
    pw, ph = width - ml - mr, height - mt - mb
    t_lo, t_hi, p_hi = -20.0, 40.0, 600.0

    def ty(v):  # 気温 → y
        return mt + ph * (t_hi - v) / (t_hi - t_lo)

    def py(v):  # 降水量 → y
        return mt + ph * (1 - min(v, p_hi) / p_hi)

    def mx(i):  # 月 (0-11) → 中心 x
        return ml + pw * (i + 0.5) / 12

    e = []
    e.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'style="max-width:{width}px;width:100%;height:auto;background:#fff" role="img" '
             f'aria-label="{name}の雨温図">')
    e.append(f'<text x="{width / 2}" y="20" text-anchor="middle" font-size="16" '
             f'font-weight="bold" {FONT}>{name}の雨温図</text>')

    # グリッドと左軸（気温）
    t = t_lo
    while t <= t_hi:
        y = ty(t)
        e.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" '
                 f'stroke="{"#999" if t == 0 else "#e3e8ee"}" stroke-width="1"/>')
        e.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="{C_TMAX}" {FONT}>{t:.0f}°C</text>')
        t += 10
    # 右軸（雨量）
    p = 0
    while p <= p_hi:
        e.append(f'<text x="{ml + pw + 6}" y="{py(p) + 4:.1f}" text-anchor="start" '
                 f'font-size="11" fill="{C_PRECIP}" {FONT}>{p:.0f}</text>')
        p += 100
    e.append(f'<text x="{ml + pw + 40}" y="{mt - 8}" text-anchor="end" font-size="11" '
             f'fill="{C_PRECIP}" {FONT}>mm</text>')

    # 降水量の棒
    bw = pw / 12 * 0.55
    for i, v in enumerate(monthly.get("precip") or []):
        if v is None:
            continue
        y = py(v)
        e.append(f'<rect x="{mx(i) - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                 f'height="{mt + ph - y:.1f}" fill="{C_PRECIP}" fill-opacity="0.85"/>')

    # 気温の線
    for key, color in (("tmax", C_TMAX), ("tavg", C_TAVG), ("tmin", C_TMIN)):
        for run in _line_runs([mx(i) for i in range(12)], monthly.get(key) or []):
            pts = [(x, ty(v)) for x, v in run]
            e.append(f'<path d="{_smooth_path(pts)}" fill="none" stroke="{color}" '
                     f'stroke-width="2.2"/>')
            for x, v in run:
                e.append(f'<circle cx="{x:.1f}" cy="{ty(v):.1f}" r="2.6" fill="{color}"/>')

    # 月ラベルと枠
    for i in range(12):
        e.append(f'<text x="{mx(i):.1f}" y="{mt + ph + 16}" text-anchor="middle" '
                 f'font-size="11" fill="#555" {FONT}>{i + 1}月</text>')
    e.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" '
             f'stroke="#c8d2dc"/>')

    # 凡例
    lx = ml + 8
    for label, color in (("最高気温", C_TMAX), ("平均気温", C_TAVG),
                         ("最低気温", C_TMIN), ("降水量", C_PRECIP)):
        e.append(f'<rect x="{lx}" y="{mt - 16}" width="10" height="10" fill="{color}"/>')
        e.append(f'<text x="{lx + 14}" y="{mt - 7}" font-size="11" fill="#333" {FONT}>{label}</text>')
        lx += 14 + len(label) * 12 + 18
    e.append("</svg>")
    return "".join(e)


def timeseries_svg(title: str, start: date, series: list[dict],
                   width: int = 640, height: int = 300) -> str:
    """日別時系列 SVG（Home の東京 30 日グラフ用）。

    series = [{label, color, values(×10 or None), width, r}]（values は同じ長さ）
    """
    ml, mr, mt, mb = 44, 12, 30, 26
    pw, ph = width - ml - mr, height - mt - mb
    n = max(len(s["values"]) for s in series)

    vals = [v for s in series for v in s["values"] if v is not None]
    if not vals:
        return ""
    lo = min(vals) / 10, max(vals) / 10
    v_lo = (int(lo[0] // 5) - 0) * 5 - 5
    v_hi = (int(lo[1] // 5) + 1) * 5 + 5

    def y(v):
        return mt + ph * (v_hi - v / 10) / (v_hi - v_lo)

    def x(i):
        return ml + pw * i / max(n - 1, 1)

    e = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
         f'style="max-width:{width}px;width:100%;height:auto;background:#fff" role="img" '
         f'aria-label="{title}">']
    t = v_lo
    while t <= v_hi:
        yy = mt + ph * (v_hi - t) / (v_hi - v_lo)
        e.append(f'<line x1="{ml}" y1="{yy:.1f}" x2="{ml + pw}" y2="{yy:.1f}" '
                 f'stroke="{"#999" if t == 0 else "#e8edf2"}"/>')
        e.append(f'<text x="{ml - 5}" y="{yy + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="#666" {FONT}>{t}</text>')
        t += 5
    for i in range(0, n, 7):
        d = start + timedelta(days=i)
        e.append(f'<text x="{x(i):.1f}" y="{mt + ph + 14}" text-anchor="middle" '
                 f'font-size="10" fill="#666" {FONT}>{d.month}/{d.day}</text>')
        e.append(f'<line x1="{x(i):.1f}" y1="{mt}" x2="{x(i):.1f}" y2="{mt + ph}" '
                 f'stroke="#f0f3f7"/>')

    for s in series:
        for run in _line_runs([x(i) for i in range(len(s["values"]))], s["values"]):
            pts = [(px, y(v)) for px, v in run]
            path = " ".join((f"M{px:.1f},{py:.1f}" if i == 0 else f"L{px:.1f},{py:.1f}")
                            for i, (px, py) in enumerate(pts))
            e.append(f'<path d="{path}" fill="none" stroke="{s["color"]}" '
                     f'stroke-width="{s.get("width", 1.4)}"/>')
            if s.get("r"):
                for px, v in run:
                    e.append(f'<circle cx="{px:.1f}" cy="{y(v):.1f}" r="{s["r"]}" '
                             f'fill="{s["color"]}"/>')
    e.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#c8d2dc"/>')
    e.append("</svg>")
    return "".join(e)
