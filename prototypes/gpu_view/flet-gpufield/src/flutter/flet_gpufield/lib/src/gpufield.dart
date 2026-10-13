// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Yasuhiro / AIseed
//
// 気象の格子データを GPU で描く部品（試作）。
//
// 色の場はシェーダー（shaders/field.frag）が画面の画素ごとに双一次補間して配色する。
// 風の粒子は Dart で動かし、前のコマを少し薄くして重ねることで尾を引かせる。
// Python から来るのは格子データと表示の決まりだけで、拡大・移動・粒子の動きはここで完結する。

import 'dart:async';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flet/flet.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

/// 正距円筒の等間隔格子（北から南の行順）の、CPU 側の値（粒子と読み取り用）。
class _Grid {
  _Grid(this.nx, this.ny, this.lon0, this.lat0, this.dlon, this.dlat, this.t,
      this.u, this.v);

  final int nx, ny;
  final double lon0, lat0, dlon, dlat;
  final Float32List t, u, v;

  /// 双一次補間。格子の外か欠測なら NaN。
  double sample(Float32List a, double lon, double lat) {
    final fi = (lon - lon0) / dlon;
    final fj = (lat0 - lat) / dlat;
    if (fi < 0 || fj < 0 || fi > nx - 1 || fj > ny - 1) return double.nan;
    var i = fi.floor();
    var j = fj.floor();
    if (i > nx - 2) i = nx - 2;
    if (j > ny - 2) j = ny - 2;
    final fx = fi - i, fy = fj - j;
    final k = j * nx + i;
    return (a[k] * (1 - fx) + a[k + 1] * fx) * (1 - fy) +
        (a[k + nx] * (1 - fx) + a[k + nx + 1] * fx) * fy;
  }
}

/// 表示の中心と縮尺（1 度あたりの論理画素）。
class _View {
  _View(this.centerLon, this.centerLat, this.pxPerDeg);

  double centerLon, centerLat, pxPerDeg;

  double lonLeft(Size s) => centerLon - s.width / 2 / pxPerDeg;
  double latTop(Size s) => centerLat + s.height / 2 / pxPerDeg;
}

class GpuFieldControl extends StatefulWidget {
  const GpuFieldControl({super.key, required this.control});

  final Control control;

  @override
  State<GpuFieldControl> createState() => _GpuFieldState();
}

class _GpuFieldState extends State<GpuFieldControl>
    with SingleTickerProviderStateMixin {
  static Future<ui.FragmentProgram>? _programFuture;

  ui.FragmentShader? _shader;
  ui.Image? _fieldImage, _lutImage, _trail;
  _Grid? _grid;
  Object? _srcField, _srcLut, _srcCoast;
  String _layer = "";
  double _vmin = double.nan, _vmax = double.nan;
  int _rebuildSeq = 0;
  double _uploadMs = 0;

  late final _View _view;
  Size _size = Size.zero;
  late final Ticker _ticker;
  final ValueNotifier<int> _repaint = ValueNotifier<int>(0);

  // 風の粒子
  static const int _maxAge = 90; // コマ。古い粒子は別の場所に生まれ直す
  static const double _pxPerMs = 0.18; // 風速 1 m/s で 1 コマに進む論理画素
  final math.Random _rnd = math.Random(7);
  int _n = -1;
  Float32List _plon = Float32List(0), _plat = Float32List(0);
  Float32List _seg = Float32List(0);
  Int32List _age = Int32List(0);
  bool _showParticles = true;
  // 尾の描き方（2026-10-13、Intel 内蔵 GPU・Linux で測定）:
  //   "vertices" 直近 _histLen コマの位置を覚え、線分を細い三角形にして 1 回で描く。
  //              粒子 12,000 で 60 fps（既定）
  //   "history"  同じ位置を線（drawRawPoints、太さ 1.2）で描く。線を三角形に分ける手間が
  //              描画スレッドの CPU に乗り、6,000 で 47 fps・12,000 で 24 fps
  //   "hairline" 線の太さ 0（1 物理画素）。history と変わらなかった
  //   "image"    前のコマを画像に焼いて薄く重ねる（Windy 型）。毎コマの焼き直しが重く 16〜20 fps
  String _trailMode = "vertices";
  static const int _histLen = 12;
  Float32List _hlon = Float32List(0), _hlat = Float32List(0); // 粒子 × _histLen の輪
  Int32List _hcount = Int32List(0); // 生まれてからの点の数（_histLen まで）
  int _head = 0;
  List<Float32List> _groupSegs = [];
  // "vertices" 用: 線分を細い四角（三角形 2 枚）にして GPU へそのまま渡す
  Float32List _vpos = Float32List(0);
  Int32List _vcol = Int32List(0);

  // 海岸線（経度・緯度の並び。切れ目は NaN）
  Float32List? _coast;
  Path? _coastPath;
  String _coastKey = "";

  Offset? _hover;
  double _scaleBase = 1.0;

  // 速さの記録
  final Stopwatch _clock = Stopwatch()..start();
  int _frames = 0;
  double _workMs = 0, _lastReport = 0;
  String _statsText = "";
  // Flutter が測った 1 コマの組み立て（build）と描画（raster）の時間
  int _timingFrames = 0;
  double _buildMs = 0, _rasterMs = 0;

  @override
  void initState() {
    super.initState();
    final c = widget.control;
    _view = _View(c.getDouble("center_lon", 137.0)!,
        c.getDouble("center_lat", 36.0)!, c.getDouble("px_per_deg", 14.0)!);
    _programFuture ??=
        ui.FragmentProgram.fromAsset('packages/flet_gpufield/shaders/field.frag');
    _programFuture!.then((p) {
      if (!mounted) return;
      setState(() => _shader = p.fragmentShader());
    });
    _ticker = createTicker(_tick)..start();
    SchedulerBinding.instance.addTimingsCallback(_onTimings);
  }

  void _onTimings(List<ui.FrameTiming> timings) {
    for (final t in timings) {
      _buildMs += t.buildDuration.inMicroseconds / 1000.0;
      _rasterMs += t.rasterDuration.inMicroseconds / 1000.0;
      _timingFrames++;
    }
  }

  @override
  void dispose() {
    SchedulerBinding.instance.removeTimingsCallback(_onTimings);
    _ticker.dispose();
    _shader?.dispose();
    _fieldImage?.dispose();
    _lutImage?.dispose();
    _trail?.dispose();
    _repaint.dispose();
    super.dispose();
  }

  /// Python から来たプロパティを見て、変わった分だけ作り直す。
  void _sync() {
    final c = widget.control;
    _showParticles = c.getBool("show_particles", true)!;
    final trail = c.getString("trail", "vertices")!;
    if (trail != _trailMode) {
      _trailMode = trail;
      _trail?.dispose();
      _trail = null;
    }
    final n = c.getInt("particles", 10000)!;
    if (n != _n) _initParticles(n);

    final coast = c.get("coast");
    if (!identical(coast, _srcCoast)) {
      _srcCoast = coast;
      _coast = coast is Uint8List ? _floats(coast) : null;
      _coastPath = null;
    }

    final field = c.get("field");
    final lut = c.get("lut");
    final layer = c.getString("layer", "t2m")!;
    final vmin = c.getDouble("vmin", -30.0)!;
    final vmax = c.getDouble("vmax", 40.0)!;
    if (!identical(field, _srcField) ||
        !identical(lut, _srcLut) ||
        layer != _layer ||
        vmin != _vmin ||
        vmax != _vmax) {
      _srcField = field;
      _srcLut = lut;
      _layer = layer;
      _vmin = vmin;
      _vmax = vmax;
      if (field is Uint8List && lut is Uint8List) _rebuild(field, lut);
    }
  }

  /// バイト列を float32 として読む（受け取った列は 4 バイト境界に揃っていないことがある）。
  static Float32List _floats(Uint8List b) {
    final copy = Uint8List.fromList(b);
    return copy.buffer.asFloat32List(0, copy.lengthInBytes ~/ 4);
  }

  static Future<ui.Image> _image(Uint8List rgba, int w, int h) {
    final done = Completer<ui.Image>();
    ui.decodeImageFromPixels(rgba, w, h, ui.PixelFormat.rgba8888, done.complete);
    return done.future;
  }

  /// 格子データを GPU 用の画像に詰める（値を 16 bit にして RG へ。欠測は A=0）。
  Future<void> _rebuild(Uint8List field, Uint8List lut) async {
    final seq = ++_rebuildSeq;
    final sw = Stopwatch()..start();
    final c = widget.control;
    final nx = c.getInt("nx", 0)!, ny = c.getInt("ny", 0)!;
    final m = nx * ny;
    if (nx < 2 || ny < 2 || field.lengthInBytes < m * 12) return;
    final all = _floats(field);
    final t = Float32List.sublistView(all, 0, m);
    final u = Float32List.sublistView(all, m, 2 * m);
    final v = Float32List.sublistView(all, 2 * m, 3 * m);
    final grid = _Grid(nx, ny, c.getDouble("lon0", 0.0)!, c.getDouble("lat0", 0.0)!,
        c.getDouble("dlon", 0.25)!, c.getDouble("dlat", 0.25)!, t, u, v);
    final wind = _layer == "wind";
    final span = (_vmax - _vmin) == 0 ? 1.0 : (_vmax - _vmin);
    final px = Uint8List(m * 4);
    for (var k = 0; k < m; k++) {
      final val = wind ? math.sqrt(u[k] * u[k] + v[k] * v[k]) : t[k];
      if (val.isNaN) continue; // A=0 のまま（欠測）
      var q = (val - _vmin) / span;
      q = q < 0 ? 0 : (q > 1 ? 1 : q);
      final iq = (q * 65535).round();
      px[k * 4] = iq >> 8;
      px[k * 4 + 1] = iq & 255;
      px[k * 4 + 3] = 255;
    }
    final fieldImg = await _image(px, nx, ny);
    final lutImg = await _image(lut, lut.lengthInBytes ~/ 4, 1);
    if (!mounted || seq != _rebuildSeq) {
      fieldImg.dispose();
      lutImg.dispose();
      return;
    }
    _uploadMs = sw.elapsedMicroseconds / 1000.0;
    setState(() {
      _fieldImage?.dispose();
      _lutImage?.dispose();
      _fieldImage = fieldImg;
      _lutImage = lutImg;
      _grid = grid;
    });
  }

  void _initParticles(int n) {
    _n = n < 0 ? 0 : n;
    _plon = Float32List(_n);
    _plat = Float32List(_n);
    _age = Int32List(_n);
    _seg = Float32List(_n * 4);
    _hlon = Float32List(_n * _histLen);
    _hlat = Float32List(_n * _histLen);
    _hcount = Int32List(_n);
    _groupSegs = List.generate(_histLen - 1, (_) => Float32List(_n * 4));
    _vpos = Float32List(_n * (_histLen - 1) * 12);
    _vcol = Int32List(_n * (_histLen - 1) * 6);
    for (var i = 0; i < _n; i++) {
      _respawn(i);
      _age[i] = _rnd.nextInt(_maxAge);
    }
  }

  void _respawn(int i) {
    final s = _size.isEmpty ? const Size(800, 600) : _size;
    _plon[i] = _view.lonLeft(s) + _rnd.nextDouble() * s.width / _view.pxPerDeg;
    _plat[i] = _view.latTop(s) - _rnd.nextDouble() * s.height / _view.pxPerDeg;
    _age[i] = 0;
    if (_hcount.length > i) {
      _hcount[i] = 1;
      _hlon[i * _histLen + _head] = _plon[i];
      _hlat[i * _histLen + _head] = _plat[i];
    }
  }

  /// 1 コマ: 粒子を風で動かし、前のコマを薄くした上に今の動きを描き足す。
  void _tick(Duration elapsed) {
    final grid = _grid;
    final s = _size;
    if (grid == null || s.isEmpty) return;
    final sw = Stopwatch()..start();
    if (_showParticles && _n > 0) {
      if (_trailMode == "image") {
        _stepImage(grid, s);
      } else {
        _stepHistory(grid, s);
      }
    }
    _workMs += sw.elapsedMicroseconds / 1000.0;
    _frames++;
    final now = _clock.elapsedMilliseconds / 1000.0;
    if (now - _lastReport >= 2.0) {
      final fps = _frames / (now - _lastReport);
      final avg = _frames > 0 ? _workMs / _frames : 0.0;
      final build = _timingFrames > 0 ? _buildMs / _timingFrames : 0.0;
      final raster = _timingFrames > 0 ? _rasterMs / _timingFrames : 0.0;
      _statsText = "${fps.toStringAsFixed(0)} fps  粒子 $_n  "
          "粒子の計算 ${avg.toStringAsFixed(2)} ms  build ${build.toStringAsFixed(1)} ms  "
          "raster ${raster.toStringAsFixed(1)} ms";
      widget.control.triggerEvent("stats", {
        "fps": fps,
        "frame_ms": avg,
        "particles": _n,
        "upload_ms": _uploadMs,
        "build_ms": build,
        "raster_ms": raster,
      });
      _frames = 0;
      _workMs = 0;
      _lastReport = now;
      _timingFrames = 0;
      _buildMs = 0;
      _rasterMs = 0;
    }
    _repaint.value++;
  }

  /// 尾を画像に焼いて重ねる方式（比べるために残す）。
  void _stepImage(_Grid grid, Size s) {
      final ppd = _view.pxPerDeg, lonL = _view.lonLeft(s), latT = _view.latTop(s);
      final w = s.width, h = s.height;
      var m = 0;
      for (var i = 0; i < _n; i++) {
        final lon = _plon[i], lat = _plat[i];
        final u = grid.sample(grid.u, lon, lat);
        final v = grid.sample(grid.v, lon, lat);
        if (u.isNaN || v.isNaN || _age[i] > _maxAge) {
          _respawn(i);
          continue;
        }
        final x0 = (lon - lonL) * ppd, y0 = (latT - lat) * ppd;
        final x1 = x0 + u * _pxPerMs, y1 = y0 - v * _pxPerMs;
        _plon[i] = lonL + x1 / ppd;
        _plat[i] = latT - y1 / ppd;
        _age[i] = (x1 < 0 || x1 > w || y1 < 0 || y1 > h) ? _maxAge + 1 : _age[i] + 1;
        _seg[m++] = x0;
        _seg[m++] = y0;
        _seg[m++] = x1;
        _seg[m++] = y1;
      }
      final rec = ui.PictureRecorder();
      final cv = Canvas(rec);
      final old = _trail;
      if (old != null) {
        cv.drawImage(old, Offset.zero,
            Paint()..color = const Color.fromRGBO(255, 255, 255, 0.93));
      }
      cv.drawRawPoints(
          ui.PointMode.lines,
          Float32List.sublistView(_seg, 0, m),
          Paint()
            ..color = const Color.fromRGBO(255, 255, 255, 0.85)
            ..strokeWidth = 1.3
            ..strokeCap = StrokeCap.round);
      _trail = rec.endRecording().toImageSync(w.ceil(), h.ceil());
      old?.dispose();
  }

  /// 粒子を動かし、位置を輪に書く（尾は paint で線にする）。
  void _stepHistory(_Grid grid, Size s) {
    final ppd = _view.pxPerDeg, lonL = _view.lonLeft(s), latT = _view.latTop(s);
    final w = s.width, h = s.height;
    _head = (_head + 1) % _histLen;
    for (var i = 0; i < _n; i++) {
      final lon = _plon[i], lat = _plat[i];
      final u = grid.sample(grid.u, lon, lat);
      final v = grid.sample(grid.v, lon, lat);
      if (u.isNaN || v.isNaN || _age[i] > _maxAge) {
        _respawn(i);
        continue;
      }
      final x1 = (lon - lonL) * ppd + u * _pxPerMs;
      final y1 = (latT - lat) * ppd - v * _pxPerMs;
      final nlon = lonL + x1 / ppd, nlat = latT - y1 / ppd;
      _plon[i] = nlon;
      _plat[i] = nlat;
      _age[i] = (x1 < 0 || x1 > w || y1 < 0 || y1 > h) ? _maxAge + 1 : _age[i] + 1;
      _hlon[i * _histLen + _head] = nlon;
      _hlat[i * _histLen + _head] = nlat;
      if (_hcount[i] < _histLen) _hcount[i]++;
    }
  }

  /// 尾を線で描く。新しい区間ほど濃く。位置は経緯度で覚えているので、拡大・移動しても尾は崩れない。
  void _paintHistory(Canvas canvas, Size s) {
    if (_trailMode == "vertices") {
      _paintVertices(canvas, s);
      return;
    }
    final ppd = _view.pxPerDeg, lonL = _view.lonLeft(s), latT = _view.latTop(s);
    final width = _trailMode == "hairline" ? 0.0 : 1.2;
    for (var a = 0; a < _histLen - 1; a++) {
      final iNew = (_head - a + _histLen) % _histLen;
      final iOld = (_head - a - 1 + _histLen) % _histLen;
      final seg = _groupSegs[a];
      var m = 0;
      for (var i = 0; i < _n; i++) {
        if (_hcount[i] < a + 2) continue;
        final b = i * _histLen;
        seg[m++] = (_hlon[b + iOld] - lonL) * ppd;
        seg[m++] = (latT - _hlat[b + iOld]) * ppd;
        seg[m++] = (_hlon[b + iNew] - lonL) * ppd;
        seg[m++] = (latT - _hlat[b + iNew]) * ppd;
      }
      if (m == 0) continue;
      final alpha = 0.9 * (1 - a / (_histLen - 1));
      canvas.drawRawPoints(
          ui.PointMode.lines,
          Float32List.sublistView(seg, 0, m),
          Paint()
            ..color = Color.fromRGBO(255, 255, 255, alpha)
            ..strokeWidth = width);
    }
  }

  /// 尾を三角形の列にして 1 回で描く（線の太さを CPU で三角形に分ける手間を省く）。
  void _paintVertices(Canvas canvas, Size s) {
    final ppd = _view.pxPerDeg, lonL = _view.lonLeft(s), latT = _view.latTop(s);
    const hw = 0.6; // 線の半分の太さ（論理画素）
    var p = 0, c = 0;
    for (var i = 0; i < _n; i++) {
      final cnt = _hcount[i];
      if (cnt < 2) continue;
      final b = i * _histLen;
      for (var a = 0; a < cnt - 1; a++) {
        final iNew = (_head - a + _histLen) % _histLen;
        final iOld = (_head - a - 1 + _histLen) % _histLen;
        final x0 = (_hlon[b + iOld] - lonL) * ppd, y0 = (latT - _hlat[b + iOld]) * ppd;
        final x1 = (_hlon[b + iNew] - lonL) * ppd, y1 = (latT - _hlat[b + iNew]) * ppd;
        final dx = x1 - x0, dy = y1 - y0;
        final len = math.sqrt(dx * dx + dy * dy);
        if (len < 1e-3) continue;
        final nx = -dy / len * hw, ny = dx / len * hw;
        // 三角形 2 枚: (p0+n, p0-n, p1+n) と (p1+n, p0-n, p1-n)
        _vpos[p++] = x0 + nx; _vpos[p++] = y0 + ny;
        _vpos[p++] = x0 - nx; _vpos[p++] = y0 - ny;
        _vpos[p++] = x1 + nx; _vpos[p++] = y1 + ny;
        _vpos[p++] = x1 + nx; _vpos[p++] = y1 + ny;
        _vpos[p++] = x0 - nx; _vpos[p++] = y0 - ny;
        _vpos[p++] = x1 - nx; _vpos[p++] = y1 - ny;
        // 古い端ほど薄く（色は頂点ごと）
        final aNew = (230 * (1 - a / (_histLen - 1))).round();
        final aOld = (230 * (1 - (a + 1) / (_histLen - 1))).round();
        final cNew = (aNew << 24) | 0xFFFFFF, cOld = (aOld << 24) | 0xFFFFFF;
        _vcol[c++] = cOld; _vcol[c++] = cOld; _vcol[c++] = cNew;
        _vcol[c++] = cNew; _vcol[c++] = cOld; _vcol[c++] = cNew;
      }
    }
    if (c == 0) return;
    final vertices = ui.Vertices.raw(ui.VertexMode.triangles,
        Float32List.sublistView(_vpos, 0, p),
        colors: Int32List.sublistView(_vcol, 0, c));
    canvas.drawVertices(vertices, BlendMode.dst, Paint());
    vertices.dispose();
  }

  void _viewChanged() {
    _trail?.dispose();
    _trail = null;
    _coastPath = null;
    _repaint.value++;
  }

  void _zoomAt(Offset pos, double factor) {
    final s = _size;
    final lon = _view.lonLeft(s) + pos.dx / _view.pxPerDeg;
    final lat = _view.latTop(s) - pos.dy / _view.pxPerDeg;
    _view.pxPerDeg = (_view.pxPerDeg * factor).clamp(2.0, 400.0);
    _view.centerLon = lon - (pos.dx - s.width / 2) / _view.pxPerDeg;
    _view.centerLat = lat + (pos.dy - s.height / 2) / _view.pxPerDeg;
    _viewChanged();
  }

  void _onScale(ScaleUpdateDetails d) {
    if (d.pointerCount >= 2 && d.scale != _scaleBase && _scaleBase > 0) {
      _zoomAt(d.localFocalPoint, d.scale / _scaleBase);
      _scaleBase = d.scale;
    }
    if (d.focalPointDelta != Offset.zero) {
      _view.centerLon -= d.focalPointDelta.dx / _view.pxPerDeg;
      _view.centerLat += d.focalPointDelta.dy / _view.pxPerDeg;
      _viewChanged();
    }
  }

  Path? _coastPathFor(Size size) {
    final coast = _coast;
    if (coast == null) return null;
    final key =
        "${_view.centerLon},${_view.centerLat},${_view.pxPerDeg},${size.width},${size.height}";
    if (_coastPath != null && key == _coastKey) return _coastPath;
    final p = Path();
    final lonL = _view.lonLeft(size), latT = _view.latTop(size), ppd = _view.pxPerDeg;
    var pen = false;
    for (var k = 0; k + 1 < coast.length; k += 2) {
      final lon = coast[k], lat = coast[k + 1];
      if (lon.isNaN || lat.isNaN) {
        pen = false;
        continue;
      }
      final x = (lon - lonL) * ppd, y = (latT - lat) * ppd;
      if (pen) {
        p.lineTo(x, y);
      } else {
        p.moveTo(x, y);
        pen = true;
      }
    }
    _coastPath = p;
    _coastKey = key;
    return p;
  }

  @override
  Widget build(BuildContext context) {
    _sync();
    return LayoutControl(
      control: widget.control,
      child: LayoutBuilder(builder: (context, cons) {
        final size = Size(cons.maxWidth.isFinite ? cons.maxWidth : 800,
            cons.maxHeight.isFinite ? cons.maxHeight : 600);
        if (size != _size) {
          _size = size;
          _trail?.dispose();
          _trail = null;
          _coastPath = null;
          // 大きさが決まる前（起動直後は 800×600 と仮定）に撒いた粒子を、今の画面全体に撒き直す
          if (_n > 0) _initParticles(_n);
        }
        return Listener(
          onPointerSignal: (e) {
            if (e is PointerScrollEvent) {
              _zoomAt(e.localPosition, math.exp(-e.scrollDelta.dy / 300));
            }
          },
          child: GestureDetector(
            onScaleStart: (_) => _scaleBase = 1.0,
            onScaleUpdate: _onScale,
            child: MouseRegion(
              onHover: (e) => _hover = e.localPosition,
              onExit: (_) => _hover = null,
              child: CustomPaint(size: size, painter: _Painter(this)),
            ),
          ),
        );
      }),
    );
  }
}

class _Painter extends CustomPainter {
  _Painter(this.s) : super(repaint: s._repaint);

  final _GpuFieldState s;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    final shader = s._shader, fieldImg = s._fieldImage, lutImg = s._lutImage;
    final grid = s._grid;
    final v = s._view;
    if (shader != null && fieldImg != null && lutImg != null && grid != null) {
      shader
        ..setFloat(0, size.width)
        ..setFloat(1, size.height)
        ..setFloat(2, v.lonLeft(size))
        ..setFloat(3, v.latTop(size))
        ..setFloat(4, 1 / v.pxPerDeg)
        ..setFloat(5, 0)
        ..setFloat(6, grid.lon0)
        ..setFloat(7, grid.lat0)
        ..setFloat(8, grid.dlon)
        ..setFloat(9, grid.dlat)
        ..setFloat(10, grid.nx.toDouble())
        ..setFloat(11, grid.ny.toDouble())
        ..setImageSampler(0, fieldImg)
        ..setImageSampler(1, lutImg);
      canvas.drawRect(rect, Paint()..shader = shader);
    } else {
      canvas.drawRect(rect, Paint()..color = const Color(0xFF212429));
    }
    final coast = s._coastPathFor(size);
    if (coast != null) {
      canvas.drawPath(
          coast,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.0
            ..color = const Color(0xDD101010));
    }
    if (s._showParticles && s._n > 0) {
      final trail = s._trail;
      if (s._trailMode == "image") {
        if (trail != null) canvas.drawImage(trail, Offset.zero, Paint());
      } else {
        s._paintHistory(canvas, size);
      }
    }
    final h = s._hover;
    if (h != null && grid != null) {
      final lon = v.lonLeft(size) + h.dx / v.pxPerDeg;
      final lat = v.latTop(size) - h.dy / v.pxPerDeg;
      final t = grid.sample(grid.t, lon, lat);
      final uu = grid.sample(grid.u, lon, lat), vv = grid.sample(grid.v, lon, lat);
      if (!t.isNaN) {
        final sp = math.sqrt(uu * uu + vv * vv);
        _label(
            canvas,
            "${lat.toStringAsFixed(2)}°N ${lon.toStringAsFixed(2)}°E  "
            "${t.toStringAsFixed(1)}℃  ${sp.toStringAsFixed(1)} m/s",
            h + const Offset(14, 14));
      }
    }
    if (s._statsText.isNotEmpty) _label(canvas, s._statsText, const Offset(8, 8));
    final attribution = s.widget.control.getString("attribution", "")!;
    if (attribution.isNotEmpty) {
      final tp = _painter(attribution, 11);
      _label(canvas, attribution,
          Offset(size.width - tp.width - 16, size.height - tp.height - 12),
          fontSize: 11);
    }
  }

  static TextPainter _painter(String text, double fontSize) => TextPainter(
        text: TextSpan(
            text: text,
            style: TextStyle(color: const Color(0xFFF0F0F0), fontSize: fontSize)),
        textDirection: TextDirection.ltr,
      )..layout();

  static void _label(Canvas canvas, String text, Offset at, {double fontSize = 12}) {
    final tp = _painter(text, fontSize);
    canvas.drawRRect(
        RRect.fromRectAndRadius(
            Rect.fromLTWH(at.dx - 4, at.dy - 2, tp.width + 8, tp.height + 4),
            const Radius.circular(4)),
        Paint()..color = const Color(0x99000000));
    tp.paint(canvas, at);
  }

  @override
  bool shouldRepaint(covariant _Painter oldDelegate) => true;
}
