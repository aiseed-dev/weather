"""SVG チャートのローカル生成（Highcharts の置き換え）。

平年値の雨温図・Home の気温推移グラフをビルド時に純 Python で SVG 化する。
外部ライブラリ依存なし。生成物はテンプレートにインライン埋め込みする。

色は旧サイトの Highcharts 設定を踏襲:
  最高気温 #F92500 / 平均気温 #008000 / 最低気温 #0C00CC / 降水量 #1987E5
"""
from __future__ import annotations

import math
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
            dash = f' stroke-dasharray="{s["dash"]}"' if s.get("dash") else ""
            e.append(f'<path d="{path}" fill="none" stroke="{s["color"]}" '
                     f'stroke-width="{s.get("width", 1.4)}"{dash}/>')
            if s.get("r"):
                for px, v in run:
                    e.append(f'<circle cx="{px:.1f}" cy="{y(v):.1f}" r="{s["r"]}" '
                             f'fill="{s["color"]}"/>')
    e.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#c8d2dc"/>')
    e.append("</svg>")
    return "".join(e)


def _nice_step(lo: float, hi: float, target: int = 6) -> float:
    """目盛り間隔を値域から決める。要素ごとに単位も桁も違うため固定にできない。

    桁は log10 で取る。文字列で数えると raw < 1 のときに指数を取り違え、
    1e-9 のような極小の刻みを返してしまう（目盛りループが数十億回まわる）。
    """
    # 全値が同一の地点（無風・湿度 100% 続きなど）は span が 0 になる。
    # そのまま計算すると刻みが極小になるので、値の大きさに応じた下限を敷く。
    span = hi - lo
    if span <= 0:
        span = max(abs(hi) * 0.02, 1.0)
    raw = span / target
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= mag * m:
            return mag * m
    return mag * 10


def intraday_svg(title: str, minutes: list[int], series: list[dict],
                 width: int = 640, height: int = 260, scale: int = 10,
                 unit: str = "", kind: str = "line") -> str:
    """当日の 10 分値時系列 SVG。

    timeseries_svg が日単位の軸なのに対し、こちらは 0 時からの分で刻む。
    アメダスは 10 分値まで出ているのに気象庁の「気温の状況」は毎正時しか
    更新しないので、ここが差になる。

    minutes … 0 時からの分（各点の x）。series の values と同じ長さ
    series  … [{label, color, values(整数 or None), width}]
    scale   … values を実単位に戻す倍率（気温 10、湿度 1 など）
    kind    … "line"（気温・気圧など）/ "bar"（降水量・日照など積算量）
    """
    ml, mr, mt, mb = 52, 12, 26, 24
    pw, ph = width - ml - mr, height - mt - mb
    vals = [v / scale for s in series for v in s["values"] if v is not None]
    if not vals or not minutes:
        return ""
    lo_v, hi_v = min(vals), max(vals)
    if kind == "bar":
        lo_v = 0                       # 積算量は 0 起点でないと大小を誤読する
    step = _nice_step(lo_v, hi_v)
    v_lo = (lo_v // step) * step - (step if kind != "bar" else 0)
    v_hi = (hi_v // step) * step + step
    if v_hi <= v_lo:
        v_hi = v_lo + step
    m_lo, m_hi = minutes[0], max(minutes[-1], minutes[0] + 1)

    def y(v):
        return mt + ph * (v_hi - v / scale) / (v_hi - v_lo)

    def x(m):
        return ml + pw * (m - m_lo) / (m_hi - m_lo)

    fmt = "{:.0f}" if step >= 1 else "{:.1f}"
    e = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
         f'style="max-width:{width}px;width:100%;height:auto" role="img" '
         f'aria-label="{title}">']
    t = v_lo
    guard = 0
    while t <= v_hi + 1e-9 and guard < 40:   # 刻みが狂っても固まらせない
        guard += 1
        yy = mt + ph * (v_hi - t) / (v_hi - v_lo)
        e.append(f'<line x1="{ml}" y1="{yy:.1f}" x2="{ml + pw}" y2="{yy:.1f}" '
                 f'stroke="{"#999" if abs(t) < 1e-9 else "#e8edf2"}"/>')
        e.append(f'<text x="{ml - 5}" y="{yy + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="#666" {FONT}>{fmt.format(t)}</text>')
        t += step
    # 3 時間ごとの目盛り。10 分値だと点が多いので目盛りは粗くする
    for hh in range(0, 25, 3):
        m = hh * 60
        if not (m_lo <= m <= m_hi):
            continue
        e.append(f'<line x1="{x(m):.1f}" y1="{mt}" x2="{x(m):.1f}" y2="{mt + ph}" '
                 f'stroke="#f0f3f7"/>')
        e.append(f'<text x="{x(m):.1f}" y="{mt + ph + 14}" text-anchor="middle" '
                 f'font-size="10" fill="#666" {FONT}>{hh}時</text>')
    if unit:
        e.append(f'<text x="{ml - 5}" y="{mt - 8}" text-anchor="end" font-size="10" '
                 f'fill="#666" {FONT}>{unit}</text>')

    if kind == "bar":
        bw = max(pw / max(len(minutes), 1) * 0.8, 1.0)
        s0 = series[0]
        for m, v in zip(minutes, s0["values"]):
            if v is None or v <= 0:
                continue
            top = y(v)
            e.append(f'<rect x="{x(m) - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                     f'height="{mt + ph - top:.1f}" fill="{s0["color"]}"/>')
    else:
        for s in series:
            for run in _line_runs([x(m) for m in minutes], s["values"]):
                pts = [(px, y(v)) for px, v in run]
                path = " ".join((f"M{px:.1f},{py:.1f}" if i == 0 else f"L{px:.1f},{py:.1f}")
                                for i, (px, py) in enumerate(pts))
                e.append(f'<path d="{path}" fill="none" stroke="{s["color"]}" '
                         f'stroke-width="{s.get("width", 1.6)}"/>')
    e.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#c8d2dc"/>')
    e.append("</svg>")
    return "".join(e)
