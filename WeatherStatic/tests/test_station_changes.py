#!/usr/bin/env python3
"""新設・廃止・移転・改番・要素変更が検知されるかを確かめる。

検知できないと黙って欠測を作る。特に改番は observations.nc の連続性が
切れるので、候補として示せることが要。
"""
import subprocess, sys
from pathlib import Path

REPO = Path.home() / "dev/weather"
import os
WS = REPO / "WeatherStatic"
ADOC = WS / "stations/amedastable.adoc"
PY = REPO / ".venv/bin/python"
if not PY.exists():
    PY = WS / ".venv/bin/python"

orig = ADOC.read_text(encoding="utf-8")
lines = orig.splitlines()

# 仕込む: 新設1 / 廃止1 / 移転1 / 要素変更1 / 改番の組1
out = []
for l in lines:
    if l.startswith("|11001 "):        # 宗谷岬 → 廃止（改番の旧側にもなる）
        continue
    if l.startswith("|11016 "):        # 稚内 → 移転(0.05度≒5km)＋要素変更
        c = l.split(" |")
        c[7] = "30.0"                  # 緯度分 24.9 → 30.0
        c[5] = "11111011"              # elems 変更
        l = " |".join(c)
    out.append(l)
# 新設(改番の新側): 宗谷岬とほぼ同じ座標の別番号
out.insert(-1, "|11002 |宗谷岬 |ソウヤミサキ |Cape Soya |C |11112010 |45 |31.2 |141 |56.1 |26")
# 純粋な新設: どこからも遠い
out.insert(-1, "|88888 |新設観測所 |シンセツ |Shinsetsu |C |11111111 |35 |0.0 |135 |0.0 |100")
ADOC.write_text("\n".join(out) + "\n", encoding="utf-8")
print("仕込み: 廃止1(11001) 移転1(11016) 要素変更1 改番候補1(11001→11002) 新設1(88888)\n")

r = subprocess.run([str(PY), "station_changes.py"], cwd=WS,
                   capture_output=True, text=True)
print(r.stdout.strip())
print(f"\n終了コード: {r.returncode}")

o = r.stdout
checks = {
    "新設を検知": "88888" in o and "新設" in o,
    "廃止を検知": "11001" in o and "廃止" in o,
    "移転を検知": "移転" in o and "11016" in o,
    "要素変更を検知": "観測要素の変更" in o,
    "改番候補を提示": "改番の候補" in o and "11002" in o,
    "派生物のずれを検知": "派生物が追いついていない" in o,
    "自動で繋がない": "自動では繋がない" in o,
}
print()
for k, v in checks.items():
    print(f"  {'OK ' if v else 'NG '} {k}")

ADOC.write_text(orig, encoding="utf-8")
prev = WS / "stations/.amedastable.prev.adoc"
if prev.exists():
    prev.write_text(orig, encoding="utf-8")
print("\n後始末: 原本と控えを戻した")
sys.exit(0 if all(checks.values()) else 1)
