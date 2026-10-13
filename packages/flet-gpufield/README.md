# flet-gpufield

気象の格子データ（等緯度経度の格子）を GPU で描く [Flet](https://flet.dev) の拡張。
AIseed Weather の地図画面の「動く地図」で使う（`docs/gpu-map.md`）。

- 色の場: シェーダー（`shaders/field.frag`）が画面の画素ごとに投影を逆にたどって
  経緯度を求め、格子を双一次補間して配色する。配色表は透明度も持ち、透ける所には陸海の地色が出る
- 風の粒子: Dart で動かし、直近 12 コマの位置を細い三角形の列にして 1 回で描く
- 等値線と数値、海岸線: 経緯度の線を受け取り、表示に合わせて描く
- 投影: `plate_carree`（全球なら経度方向につながる）、`polar_north`・`polar_south`（正距方位）
- 拡大（ホイール・ピンチ）・移動（ドラッグ）・読み取りは部品の中で完結する

## Python から

```python
from flet_gpufield import GpuField, GpuFrame

GpuField(
    nx=361, ny=281, lon0=95.0, lat0=65.0, dlon=0.25, dlat=0.25,
    projection="plate_carree", center_lon=140, center_lat=30, px_per_deg=14,
    vmin=-20, vmax=40, lut=lut_256_rgba, base=land_mask_bytes, coast=coast_bytes,
    label="2m気温", unit="°C",
    frames=[GpuFrame(index=i, scalar=..., wind=..., contours=..., contours_bold=..., labels=...)
            for i in ...],
    step=0,
    attribution="Data: ECMWF Open Data (CC-BY-4.0)",
    on_ready=..., on_stats=...,
)
```

データの形は `src/flet_gpufield/gpufield.py` の説明のとおり（値は 16 bit を RGBA の RG に、
風は int16 の 0.01 m/s、線は float32 の (経度, 緯度) で切れ目は NaN）。
時刻は `GpuFrame` を足していき、`step` を変えるだけで切り替える。

## 実行環境

自作の部品は Flet の出来合いの実行環境では表示されない。リポジトリの
`tools/build_client.sh` で部品入りの実行環境を `build/linux` に作り、プロジェクトの
フォルダーで `flet run` する（Linux の Flet は `build/linux` を先に使う）。
部品（Dart・シェーダー）を変えたら作り直す。

Linux（Intel 内蔵 GPU）で 57〜59 fps（粒子 10,000、等値線・海岸線あり）。Web でも描ける。
