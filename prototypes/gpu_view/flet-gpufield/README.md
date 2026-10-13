# flet-gpufield（試作）

気象の格子データ（正距円筒の等間隔格子）を GPU で描く [Flet](https://flet.dev) の拡張。
色の場はシェーダーで画素ごとに補間・配色し、風の粒子は Dart で動かして三角形の列で描く。
Python からは格子データと表示の決まりを渡すだけ。使い方と測定は `../README.md`。

- Python 側: `src/flet_gpufield/gpufield.py`（`GpuField`、`GpuFieldStatsEvent`）
- Flutter 側: `src/flutter/flet_gpufield/lib/src/gpufield.dart`、`shaders/field.frag`

Linux（Intel 内蔵 GPU）と Web で動作を確かめた。
