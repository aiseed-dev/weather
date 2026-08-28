#!/usr/bin/env python3
"""観測所の変更が git の差分として読めるかを確かめる。

検知できないと黙って欠測を作るので、新設・廃止・変更の3種を仕込んで
(1) watch_stations.py が報告するか (2) git diff で読めるか を見る。
"""
import json, subprocess, sys
from pathlib import Path

REPO = Path.home() / "dev/weather"
WS = REPO / "WeatherStatic"
TABLE = WS / "stations/amedastable.json"


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True).stdout


d = json.loads(TABLE.read_text(encoding="utf-8"))
del d["11001"]                                   # 次回は「新設」として現れる
d["99999"] = {"kjName": "架空観測所", "type": "C", "elems": "11111111",
              "lat": [35, 0], "lon": [135, 0], "alt": 10}      # 「廃止」
d["11016"]["elems"] = "00000000"                 # 「変更」
d["11016"]["kjName"] = "稚内(旧名)"
TABLE.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                 encoding="utf-8")
print("差分を3種仕込んだ（新設/廃止/変更）\n")

r = subprocess.run([str(REPO / ".venv/bin/python"), "watch_stations.py"],
                   cwd=WS, capture_output=True, text=True)
print(r.stdout.strip())
print(f"\n終了コード: {r.returncode}（変更ありなら 1）")

out = r.stdout
ok_report = ("**新設** 11001" in out and "**廃止** 99999" in out
             and "elems: 00000000" in out and r.returncode == 1)
print(f"報告できた: {ok_report}")

# git の差分として読めるか
stat = git("diff", "--stat", "--", "WeatherStatic/stations/")
diff = git("diff", "-U0", "--", "WeatherStatic/stations/amedastable.json")
print(f"\ngit diff --stat:\n  {stat.strip().splitlines()[0] if stat.strip() else '(差分なし)'}")
lines = [l for l in diff.splitlines() if l.startswith(("+", "-"))
         and not l.startswith(("+++", "---"))]
print(f"変更行: {len(lines)} 行")
for l in lines[:6]:
    print(f"  {l[:78]}")
ok_git = len(lines) > 0 and any("11001" in l or "宗谷岬" in l for l in lines)
print(f"\ngit の差分として読める: {ok_git}")

git("checkout", "--", "WeatherStatic/stations/")
print("後始末: git checkout で戻した")
sys.exit(0 if (ok_report and ok_git) else 1)
