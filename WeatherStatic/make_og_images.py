# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
"""共有（Facebook など）で出る絵 wwwroot/Images/Og/site.jpg と app.jpg を作る。

一度だけ使う道具で、generate.py のたびには流さない。作った JPEG もコミットする。
作り直すとき:  ../.venv/bin/python make_og_images.py

字体は Noto Sans CJK JP（/usr/share/fonts/opentype/noto/NotoSansCJK-{Regular,Bold}.ttc。
.ttc の先頭が JP）。無ければ `fc-list :lang=ja` で探して FONT_REGULAR / FONT_BOLD を直す。

site.jpg の雨温図は、気象庁の東京の平年値（1991–2020）。値は deb2 の
master/normals/47662.json（0.1 単位）と 2026-10-18 に照合して一致した。
色と見かけは weatherlib/svgchart.py の uonzu_svg（平均気温 #008000、降水量 #1987E5）に揃える。
Facebook は絵を切り詰めて出すことがあるので、文字は端から 60px 以上離す。
"""
from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
OUT = BASE / "wwwroot" / "Images" / "Og"
FORECAST_MAP = BASE / "wwwroot" / "Images" / "App" / "forecast-map.jpg"

W, H = 1200, 630
MARGIN = 64   # 端から 60px 以上

FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

ACCENT_DARK = "#0d47a1"     # assets/site.css の --accent-dark
ACCENT_LIGHT = "#e3f0fb"    # --accent-light
SKY_FROM, SKY_TO = "#1565c0", "#4fa3e3"
C_TAVG, C_PRECIP = "#008000", "#1987E5"   # weatherlib/svgchart.py

# 東京の平年値 1991–2020（気象庁）
TOKYO_TEMP = [5.4, 6.1, 9.4, 14.3, 18.8, 21.9, 25.7, 26.9, 23.3, 18.0, 12.5, 7.7]
TOKYO_PRECIP = [59.7, 56.5, 116.0, 133.7, 139.7, 167.8, 156.2, 154.7, 224.9, 234.8, 96.3, 57.9]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def climograph(width: int, height: int) -> Image.Image:
    """東京の雨温図（月平均気温の折れ線＋月降水量の棒）。凡例なし、目盛りは小さく。"""
    fp = font_manager.FontProperties(fname=FONT_REGULAR)
    scale = 3   # 縮めて描くと線と字がなめらかになる
    fig = plt.figure(figsize=(width * scale / 300, height * scale / 300), dpi=300)
    ax = fig.add_axes([0.13, 0.11, 0.74, 0.84])
    months = list(range(1, 13))
    ax.bar(months, TOKYO_PRECIP, width=0.62, color=C_PRECIP, zorder=2)
    ax.set_ylim(0, 300)           # 降水量と気温を 10:1 に（サイトの図と同じ比）
    ax.set_xlim(0.4, 12.6)
    ax.set_yticks([0, 100, 200, 300])
    ax.tick_params(axis="y", labelsize=15, colors=C_PRECIP, length=0)
    ax.set_xticks(months)
    ax.set_xticklabels([str(m) for m in months], fontproperties=fp, fontsize=12)
    ax.tick_params(axis="x", length=0, colors="#444")
    ax.grid(axis="y", color="#e3e8ee", zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#999")
    ax2 = ax.twinx()
    ax2.plot(months, TOKYO_TEMP, color=C_TAVG, linewidth=3.2, marker="o", markersize=5, zorder=3)
    ax2.set_ylim(0, 30)
    ax2.set_yticks([0, 10, 20, 30])
    ax2.tick_params(axis="y", labelsize=15, colors=C_TAVG, length=0)
    for side in ("top", "left", "bottom", "right"):
        ax2.spines[side].set_visible(False)
    for label in ax.get_yticklabels() + ax2.get_yticklabels():
        label.set_fontproperties(fp)
        label.set_fontsize(15)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGBA")
    return img.resize((width, height), Image.LANCZOS)


def make_site() -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 18, H], fill=ACCENT_DARK)             # 左の帯
    d.rectangle([W - 18, 0, W, H], fill=SKY_TO)              # 右の細い帯

    x = MARGIN + 14
    d.text((x, 154), "個人開発気象統計", font=font(68, bold=True), fill=ACCENT_DARK)
    sub = font(31)
    lines = ["アメダスの今日の気温・10分ごとの実況", "暑さ寒さのランキング・雨温図"]
    for i, line in enumerate(lines):
        d.text((x, 290 + i * 52), line, font=sub, fill="#2c3e50")
    d.text((x, 470), "weather.time-j.net", font=font(30), fill="#5b6b7b")

    # 右の 4 割: 東京の雨温図（文字は端から 60px 以上）
    cw, ch = 400, 380
    cx, cy = W - MARGIN - cw, 110
    d.rounded_rectangle([cx - 20, cy - 24, cx + cw + 20, cy + ch + 66], radius=16,
                        fill="#f6f9fc", outline="#d5dde6", width=2)
    img.paste(climograph(cw, ch), (cx, cy), climograph(cw, ch))
    d.text((cx + cw / 2, cy + ch + 34), "東京（平年値 1991–2020）", font=font(22),
           fill="#2c3e50", anchor="mm")
    return img


def make_app() -> Image.Image:
    # 画面の写真のうち、左の欄（古い英語併記の文字が写っている）を除いた地図の部分を、
    # 幅いっぱいに拡大して使う
    shot = Image.open(FORECAST_MAP).convert("RGB")
    map_part = shot.crop((188, 66, 800, 372))                       # 612×306。タイトルの文字は除く
    scale = H / map_part.height
    scaled = map_part.resize((round(map_part.width * scale), H), Image.LANCZOS)
    left = (scaled.width - W) // 2
    img = scaled.crop((left, 0, left + W, H))

    band_h = 224
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle([0, H - band_h, W, H], fill=(10, 24, 48, 222))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(img)
    d.text((MARGIN, H - band_h + 22), "AIseed Weather", font=font(72, bold=True), fill="white")
    d.text((MARGIN, H - band_h + 119), "数値予報の天気図を、自分のパソコンで描く（ECMWF・無料）",
           font=font(34), fill="#dbe9f7")
    return img


def save(img: Image.Image, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    img.save(path, "JPEG", quality=85, optimize=True, progressive=True)
    print(f"{path.relative_to(BASE)}  {img.size[0]}x{img.size[1]}  {path.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    save(make_site(), "site.jpg")
    save(make_app(), "app.jpg")
