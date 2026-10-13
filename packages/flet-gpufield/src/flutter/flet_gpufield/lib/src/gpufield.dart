// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Yasuhiro / AIseed
//
// 気象の格子データを GPU で描く部品。
//
// 色の場はシェーダー（shaders/field.frag）が画面の画素ごとに投影を逆にたどって双一次補間
// （階級のような値は最寄りの格子点）して配色し、風の粒子は Dart で動かして細い三角形の列で描く。
// 時刻ごとのデータは子の GpuFrame で受け取り、step（番号）を変えるだけで切り替える。
// Python から来るのはデータと表示の決まりだけで、拡大・移動・粒子の動きはここで完結する。

import 'dart:async';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flet/flet.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

/// 等緯度経度の格子（北から南の行順）。
class _Grid {
  _Grid(this.nx, this.ny, this.lon0, this.lat0, this.dlon, this.dlat, this.periodic);

  final int nx, ny;
  final double lon0, lat0, dlon, dlat;
  final bool periodic;

  bool same(_Grid o) =>
      nx == o.nx &&
      ny == o.ny &&
      lon0 == o.lon0 &&
      lat0 == o.lat0 &&
      dlon == o.dlon &&
      dlat == o.dlat &&
      periodic == o.periodic;

  /// 経緯度を格子の (列, 行) の小数位置に。格子の外なら false。
  bool locate(double lon, double lat, List<double> out) {
    var fi = (lon - lon0) / dlon;
    if (periodic) {
      fi = ((lon - lon0) % 360.0 + 360.0) % 360.0 / dlon;
    }
    final fj = (lat0 - lat) / dlat;
    if (fj < 0 || fj > ny - 1) return false;
    if (periodic ? (fi < 0 || fi >= nx) : (fi < 0 || fi > nx - 1)) return false;
    out[0] = fi;
    out[1] = fj;
    return true;
  }
}

/// 1 時刻分のデータ。値は 16 bit に詰めた RGBA（シェーダーへそのまま渡す）か、
/// 1 格子点 1 バイト（u8。細かい格子の階級をそのまま送ると RGBA の 4 分の 1 で済む）。
/// 風は int16（0.01 m/s 単位）の u・v。
class _Frame {
  _Frame(this.rgba, this.u8, this.u, this.v, this.contours, this.bold, this.labels);

  final Uint8List? rgba, u8;
  final Int16List? u, v;
  final Float32List? contours, bold, labels;

  /// その格子点の値（0〜1）。欠測は NaN。
  double q(int k) {
    final b = u8;
    if (b != null) return b[k] / 255.0;
    final c = rgba!;
    if (c[k * 4 + 3] < 128) return double.nan;
    return (c[k * 4] * 256 + c[k * 4 + 1]) / 65535.0;
  }

  /// シェーダーに渡す RGBA。1 バイトの値 b は 16 bit の b × 257（= b / 255）にする。
  /// GPU に置く時刻だけ作る（全時刻を RGBA で持つと、細かい格子では数百 MB になる）。
  Uint8List shaderRgba() {
    final b = u8;
    if (b == null) return rgba!;
    final out = Uint8List(b.length * 4);
    for (var k = 0; k < b.length; k++) {
      out[k * 4] = b[k];
      out[k * 4 + 1] = b[k];
      out[k * 4 + 3] = 255;
    }
    return out;
  }
}

/// 双一次補間（格子の位置 fi, fj で、値の取り出し方を渡す）。
double _bilinear(_Grid g, double fi, double fj, double Function(int k) at) {
  var i0 = fi.floor();
  var j0 = fj.floor();
  if (j0 > g.ny - 2) j0 = g.ny - 2;
  int i1;
  double fx;
  if (g.periodic) {
    fx = fi - i0;
    i0 = i0 % g.nx;
    i1 = (i0 + 1) % g.nx;
  } else {
    if (i0 > g.nx - 2) i0 = g.nx - 2;
    i1 = i0 + 1;
    fx = fi - i0;
  }
  final fy = fj - j0;
  final r0 = j0 * g.nx, r1 = (j0 + 1) * g.nx;
  return (at(r0 + i0) * (1 - fx) + at(r0 + i1) * fx) * (1 - fy) +
      (at(r1 + i0) * (1 - fx) + at(r1 + i1) * fx) * fy;
}

/// 最寄りの格子点の番号（階級のような値は補間しない）。
int _nearestIndex(_Grid g, double fi, double fj) {
  var i = fi.round();
  i = g.periodic ? i % g.nx : math.min(i, g.nx - 1);
  return math.min(fj.round(), g.ny - 1) * g.nx + i;
}

/// 投影と表示の位置。0: 正距円筒、1: 北極の正距方位、2: 南極の正距方位。
class _Proj {
  _Proj(this.mode, this.centerLon, this.centerLat, this.ppd);

  final int mode;
  double centerLon, centerLat; // 正距円筒: 画面中央の経緯度。極: centerLon が真下に来る経度
  double ppd; // 1 度あたりの論理画素（極では余緯度 1 度あたり）
  double offX = 0, offY = 0; // 極: 画面中央からの極のずれ

  double lonLeft(Size s) => centerLon - s.width / 2 / ppd;
  double latTop(Size s) => centerLat + s.height / 2 / ppd;
  double poleX(Size s) => s.width / 2 + offX;
  double poleY(Size s) => s.height / 2 + offY;

  String key(Size s) => "$mode,$centerLon,$centerLat,$ppd,$offX,$offY,${s.width},${s.height}";

  /// 経緯度 → 画面。正距円筒で経度がつながる格子なら、画面の左端から 360 度の範囲に寄せる。
  void toScreen(double lon, double lat, Size s, bool periodic, List<double> out) {
    if (mode == 0) {
      final left = lonLeft(s);
      var x = lon - left;
      if (periodic) x = (x % 360.0 + 360.0) % 360.0;
      out[0] = x * ppd;
      out[1] = (latTop(s) - lat) * ppd;
      return;
    }
    final north = mode == 1;
    final r = (north ? 90.0 - lat : lat + 90.0) * ppd;
    final ang = (north ? lon - centerLon : centerLon - lon) * math.pi / 180.0;
    out[0] = poleX(s) + r * math.sin(ang);
    out[1] = poleY(s) + r * math.cos(ang);
  }

  /// 画面 → 経緯度。地球の外なら false。
  bool toLonLat(double x, double y, Size s, List<double> out) {
    if (mode == 0) {
      out[0] = lonLeft(s) + x / ppd;
      out[1] = latTop(s) - y / ppd;
      return out[1] <= 90 && out[1] >= -90;
    }
    final dx = x - poleX(s), dy = y - poleY(s);
    final r = math.sqrt(dx * dx + dy * dy) / ppd;
    if (r > 180) return false;
    final ang = math.atan2(dx, dy) * 180.0 / math.pi;
    if (mode == 1) {
      out[0] = centerLon + ang;
      out[1] = 90.0 - r;
    } else {
      out[0] = centerLon - ang;
      out[1] = -90.0 + r;
    }
    return true;
  }

  /// その経度での東向き・北向きの単位ベクトル（画面の座標、y は下向き）。
  void basis(double lon, List<double> out) {
    if (mode == 0) {
      out[0] = 1;
      out[1] = 0;
      out[2] = 0;
      out[3] = -1;
      return;
    }
    if (mode == 1) {
      final a = (lon - centerLon) * math.pi / 180.0;
      out[0] = math.cos(a);
      out[1] = -math.sin(a);
      out[2] = -math.sin(a);
      out[3] = -math.cos(a);
    } else {
      final a = (centerLon - lon) * math.pi / 180.0;
      out[0] = -math.cos(a);
      out[1] = math.sin(a);
      out[2] = math.sin(a);
      out[3] = math.cos(a);
    }
  }

  void pan(Offset d) {
    if (mode == 0) {
      centerLon -= d.dx / ppd;
      centerLat = (centerLat + d.dy / ppd).clamp(-90.0, 90.0);
    } else {
      offX += d.dx;
      offY += d.dy;
    }
  }

  void zoomAt(Offset p, double factor, Size s) {
    final nppd = (ppd * factor).clamp(1.0, 400.0);
    final f = nppd / ppd;
    if (mode == 0) {
      final lon = lonLeft(s) + p.dx / ppd, lat = latTop(s) - p.dy / ppd;
      ppd = nppd;
      centerLon = lon - (p.dx - s.width / 2) / ppd;
      centerLat = lat + (p.dy - s.height / 2) / ppd;
    } else {
      // 指の下の点を動かさない: 極の位置をカーソルから f 倍に
      final px = poleX(s), py = poleY(s);
      final nx = p.dx + (px - p.dx) * f, ny = p.dy + (py - p.dy) * f;
      offX = nx - s.width / 2;
      offY = ny - s.height / 2;
      ppd = nppd;
    }
  }

  void setUniform(ui.FragmentShader sh, Size s) {
    if (mode == 0) {
      sh
        ..setFloat(2, lonLeft(s))
        ..setFloat(3, latTop(s))
        ..setFloat(4, 1 / ppd)
        ..setFloat(5, 0);
    } else {
      sh
        ..setFloat(2, poleX(s))
        ..setFloat(3, poleY(s))
        ..setFloat(4, 1 / ppd)
        ..setFloat(5, centerLon);
    }
  }
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
  static const int _textureCache = 8; // GPU に置いておく時刻の数

  ui.FragmentShader? _shader;
  String _shaderError = "";
  ui.Image? _lutImage, _baseImage;
  Object? _srcLut, _srcBase, _srcCoast;
  _Grid? _grid;
  _Proj? _proj;
  String _projKey = "";

  // 時刻ごとのデータ（番号 → データ）と GPU の画像
  final Map<int, _Frame> _frames = {};
  final Map<int, Object?> _frameSrc = {};
  final Map<int, ui.Image> _textures = {};
  final List<int> _textureOrder = [];
  final Set<int> _pending = {};
  int _step = 0;
  int _shown = -1; // いま描いている時刻（step の画像ができるまで前のを出す）
  double _uploadMs = 0;

  Size _size = Size.zero;
  late final Ticker _ticker;
  final ValueNotifier<int> _repaint = ValueNotifier<int>(0);

  // 風の粒子
  static const int _maxAge = 90;
  static const double _pxPerMs = 0.18; // 風速 1 m/s で 1 コマに進む論理画素
  static const int _histLen = 12;
  final math.Random _rnd = math.Random(7);
  int _n = -1;
  Float32List _plon = Float32List(0), _plat = Float32List(0);
  Float32List _hlon = Float32List(0), _hlat = Float32List(0);
  // 同じ輪の画面の位置。表示が変わらない間は投影を計算し直さずに尾を描ける
  // （毎コマ 粒子 × 12 点を投影し直すと、それだけで 1 コマ数ミリ秒かかった）
  Float32List _hx = Float32List(0), _hy = Float32List(0);
  String _histViewKey = "";
  Int32List _age = Int32List(0), _hcount = Int32List(0);
  int _head = 0;
  Float32List _vpos = Float32List(0);
  Int32List _vcol = Int32List(0);
  bool _showParticles = true;

  // 値が階級のとき: 最寄りの格子点で塗り、読み取りには階級の名前を出す
  bool _nearest = false;
  List<String> _classes = const [];

  // 海岸線・等値線（経緯度の並び。切れ目は NaN）と、描いた線の置き場
  Float32List? _coast;
  Path? _coastPath;
  String _coastKey = "";
  Path? _contourPath, _boldPath;
  String _contourKey = "";

  Offset? _hover;
  double _scaleBase = 1.0;
  final List<double> _xy = [0, 0], _ll = [0, 0], _ij = [0, 0], _b = [0, 0, 0, 0];

  // 速さの記録
  final Stopwatch _clock = Stopwatch()..start();
  int _frameCount = 0, _timingFrames = 0;
  double _workMs = 0, _lastReport = 0, _buildMs = 0, _rasterMs = 0;

  @override
  void initState() {
    super.initState();
    _programFuture ??=
        ui.FragmentProgram.fromAsset('packages/flet_gpufield/shaders/field.frag');
    _programFuture!.then((p) {
      if (!mounted) return;
      setState(() => _shader = p.fragmentShader());
    }).catchError((Object e) {
      // シェーダーが読めない・訳せない実行環境では、色の場の代わりに理由を出す
      debugPrint("GpuField: shader failed: $e");
      if (mounted) setState(() => _shaderError = "$e");
    });
    _ticker = createTicker(_tick)..start();
    SchedulerBinding.instance.addTimingsCallback(_onTimings);
    // Python 側が「この実行環境に部品がある」と分かるように知らせる
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) widget.control.triggerEvent("ready");
    });
  }

  @override
  void dispose() {
    SchedulerBinding.instance.removeTimingsCallback(_onTimings);
    _ticker.dispose();
    _shader?.dispose();
    _lutImage?.dispose();
    _baseImage?.dispose();
    _vectorImage?.dispose();
    _interactTimer?.cancel();
    for (final t in _textures.values) {
      t.dispose();
    }
    _repaint.dispose();
    super.dispose();
  }

  void _onTimings(List<ui.FrameTiming> timings) {
    for (final t in timings) {
      _buildMs += t.buildDuration.inMicroseconds / 1000.0;
      _rasterMs += t.rasterDuration.inMicroseconds / 1000.0;
      _timingFrames++;
    }
  }

  static Float32List? _floats(Object? b) {
    if (b is! Uint8List || b.isEmpty) return null;
    final copy = Uint8List.fromList(b);
    return copy.buffer.asFloat32List(0, copy.lengthInBytes ~/ 4);
  }

  static Int16List? _int16s(Uint8List b, int offsetBytes, int count) {
    if (b.lengthInBytes < offsetBytes + count * 2) return null;
    final copy = Uint8List.fromList(Uint8List.sublistView(b, offsetBytes, offsetBytes + count * 2));
    return copy.buffer.asInt16List(0, count);
  }

  static Future<ui.Image> _image(Uint8List rgba, int w, int h) {
    final done = Completer<ui.Image>();
    ui.decodeImageFromPixels(rgba, w, h, ui.PixelFormat.rgba8888, done.complete);
    return done.future;
  }

  void _dropTextures() {
    for (final t in _textures.values) {
      t.dispose();
    }
    _textures.clear();
    _textureOrder.clear();
    _shown = -1;
  }

  /// Python から来たプロパティを見て、変わった分だけ作り直す。
  void _sync() {
    final c = widget.control;
    final grid = _Grid(
      c.getInt("nx", 0)!,
      c.getInt("ny", 0)!,
      c.getDouble("lon0", 0.0)!,
      c.getDouble("lat0", 0.0)!,
      c.getDouble("dlon", 0.25)!,
      c.getDouble("dlat", 0.25)!,
      c.getBool("periodic", false)!,
    );
    if (_grid == null || !_grid!.same(grid)) {
      _grid = grid;
      _frames.clear();
      _frameSrc.clear();
      _dropTextures();
      _srcBase = null;
    }

    final projName = c.getString("projection", "plate_carree")!;
    final mode = projName == "polar_north" ? 1 : (projName == "polar_south" ? 2 : 0);
    final projKey = "$mode,${c.getDouble("center_lon", 137.0)},"
        "${c.getDouble("center_lat", 36.0)},${c.getDouble("px_per_deg", 14.0)}";
    if (projKey != _projKey) {
      _projKey = projKey;
      _proj = _Proj(mode, c.getDouble("center_lon", 137.0)!, c.getDouble("center_lat", 36.0)!,
          c.getDouble("px_per_deg", 14.0)!);
      _viewChanged(respawn: true);
    }

    _showParticles = c.getBool("show_particles", true)!;
    _nearest = c.getString("sampling", "bilinear") == "nearest";
    final classes = c.get("classes");
    _classes = classes is List ? [for (final x in classes) "$x"] : const [];
    final n = c.getInt("particles", 10000)!;
    if (n != _n) _initParticles(n);

    final coast = c.get("coast");
    if (!identical(coast, _srcCoast)) {
      _srcCoast = coast;
      _coast = _floats(coast);
      _coastPath = null;
    }

    final lut = c.get("lut");
    if (!identical(lut, _srcLut) && lut is Uint8List && lut.lengthInBytes >= 8) {
      _srcLut = lut;
      _image(lut, lut.lengthInBytes ~/ 4, 1).then((img) {
        if (!mounted) {
          img.dispose();
          return;
        }
        setState(() {
          _lutImage?.dispose();
          _lutImage = img;
        });
      });
    }

    final base = c.get("base");
    if (!identical(base, _srcBase)) {
      _srcBase = base;
      if (base is Uint8List && base.lengthInBytes >= grid.nx * grid.ny) {
        final px = Uint8List(grid.nx * grid.ny * 4);
        for (var k = 0; k < grid.nx * grid.ny; k++) {
          px[k * 4] = base[k] > 0 ? 255 : 0;
          px[k * 4 + 3] = 255;
        }
        _image(px, grid.nx, grid.ny).then((img) {
          if (!mounted) {
            img.dispose();
            return;
          }
          setState(() {
            _baseImage?.dispose();
            _baseImage = img;
          });
        });
      } else {
        _baseImage?.dispose();
        _baseImage = null;
      }
    }

    // 時刻ごとのデータ（子の GpuFrame）
    final m = grid.nx * grid.ny;
    final seen = <int>{};
    for (final f in c.children("frames", visibleOnly: false)) {
      // 既定値（0）のプロパティは Flet が送らないので、既定は Python 側と同じ 0 にする
      // （-1 にしていたら、0 番の時刻だけ届かないことになっていた）
      final idx = f.getInt("index", 0)!;
      seen.add(idx);
      final scalar = f.get("scalar"), scalar8 = f.get("scalar8");
      final src = scalar8 is Uint8List ? scalar8 : scalar;
      if (identical(src, _frameSrc[idx]) && _frames.containsKey(idx)) continue;
      final rgba = scalar is Uint8List && scalar.lengthInBytes >= m * 4 ? scalar : null;
      final u8 = scalar8 is Uint8List && scalar8.lengthInBytes >= m ? scalar8 : null;
      if (rgba == null && u8 == null) continue;
      _frameSrc[idx] = src;
      final wind = f.get("wind");
      Int16List? u, v;
      if (wind is Uint8List) {
        u = _int16s(wind, 0, m);
        v = _int16s(wind, m * 2, m);
      }
      _frames[idx] = _Frame(u8 == null ? rgba : null, u8, u, v, _floats(f.get("contours")),
          _floats(f.get("contours_bold")), _floats(f.get("labels")));
      _evict(idx);
      if (idx == _shown) _shown = -1;
    }
    for (final idx in _frames.keys.toList()) {
      if (!seen.contains(idx)) {
        _frames.remove(idx);
        _frameSrc.remove(idx);
        _evict(idx);
      }
    }

    final step = c.getInt("step", 0)!;
    if (step != _step) {
      _step = step;
      _contourPath = null;
    }
    _ensureTexture(_step);
    _ensureTexture(_step + 1); // 次の時刻も先に GPU へ
  }

  void _evict(int idx) {
    final t = _textures.remove(idx);
    t?.dispose();
    _textureOrder.remove(idx);
  }

  void _ensureTexture(int idx) {
    final grid = _grid;
    final f = _frames[idx];
    if (grid == null || f == null || _textures.containsKey(idx) || _pending.contains(idx)) {
      return;
    }
    _pending.add(idx);
    final sw = Stopwatch()..start();
    _image(f.shaderRgba(), grid.nx, grid.ny).then((img) {
      _pending.remove(idx);
      if (!mounted || !identical(_frames[idx], f)) {
        img.dispose();
        return;
      }
      _textures[idx] = img;
      _textureOrder.add(idx);
      while (_textureOrder.length > _textureCache) {
        final old = _textureOrder.firstWhere((k) => k != _step, orElse: () => -1);
        if (old < 0) break;
        _evict(old);
      }
      if (idx == _step) _uploadMs = sw.elapsedMicroseconds / 1000.0;
      _repaint.value++;
    });
  }

  /// いま描く時刻。step の画像がまだ無ければ、前に描いていた時刻のまま。
  int get _current {
    if (_textures.containsKey(_step)) {
      if (_shown != _step) {
        _shown = _step;
        _contourPath = null;
      }
      return _step;
    }
    return _textures.containsKey(_shown) ? _shown : -1;
  }

  void _initParticles(int n) {
    _n = n < 0 ? 0 : n;
    _plon = Float32List(_n);
    _plat = Float32List(_n);
    _age = Int32List(_n);
    _hlon = Float32List(_n * _histLen);
    _hlat = Float32List(_n * _histLen);
    _hx = Float32List(_n * _histLen);
    _hy = Float32List(_n * _histLen);
    _histViewKey = "";
    _hcount = Int32List(_n);
    _vpos = Float32List(_n * (_histLen - 1) * 12);
    _vcol = Int32List(_n * (_histLen - 1) * 6);
    for (var i = 0; i < _n; i++) {
      _respawn(i);
      _age[i] = _rnd.nextInt(_maxAge);
    }
  }

  void _respawn(int i) {
    final proj = _proj;
    final s = _size.isEmpty ? const Size(800, 600) : _size;
    if (proj == null) return;
    var x = 0.0, y = 0.0;
    for (var tries = 0; tries < 4; tries++) {
      x = _rnd.nextDouble() * s.width;
      y = _rnd.nextDouble() * s.height;
      if (proj.toLonLat(x, y, s, _ll)) break;
    }
    _plon[i] = _ll[0];
    _plat[i] = _ll[1];
    _age[i] = 0;
    _hcount[i] = 1;
    final k = i * _histLen + _head;
    _hlon[k] = _ll[0];
    _hlat[k] = _ll[1];
    _hx[k] = x;
    _hy[k] = y;
  }

  /// 1 コマ: 粒子を風で動かし、位置を輪に書く（尾は paint で三角形の列にする）。
  void _tick(Duration elapsed) {
    final grid = _grid, proj = _proj;
    final s = _size;
    final f = _frames[_current];
    if (grid == null || proj == null || s.isEmpty) return;
    final sw = Stopwatch()..start();
    if (_showParticles && _n > 0 && f != null && f.u != null && f.v != null) {
      final u16 = f.u!, v16 = f.v!;
      final viewKey = proj.key(s);
      final sameView = viewKey == _histViewKey;
      final prev = _head;
      _head = (_head + 1) % _histLen;
      for (var i = 0; i < _n; i++) {
        final lon = _plon[i], lat = _plat[i];
        if (_age[i] > _maxAge || !grid.locate(lon, lat, _ij)) {
          _respawn(i);
          continue;
        }
        final u = _sample16(grid, _ij[0], _ij[1], u16);
        final v = _sample16(grid, _ij[0], _ij[1], v16);
        final kp = i * _histLen + prev;
        double x0, y0;
        if (sameView) {
          x0 = _hx[kp];
          y0 = _hy[kp];
        } else {
          proj.toScreen(lon, lat, s, grid.periodic, _xy);
          x0 = _xy[0];
          y0 = _xy[1];
        }
        proj.basis(lon, _b);
        final x1 = x0 + (u * _b[0] + v * _b[2]) * _pxPerMs;
        final y1 = y0 + (u * _b[1] + v * _b[3]) * _pxPerMs;
        final k = i * _histLen + _head;
        if (x1 < 0 || x1 > s.width || y1 < 0 || y1 > s.height || !proj.toLonLat(x1, y1, s, _ll)) {
          // 画面から出た粒子は次のコマで生まれ直す。このコマの輪の位置も今の位置で埋めておく
          // （埋めないと、生まれ直す前の遠い位置が残っていて、尾が長い直線になる）
          _age[i] = _maxAge + 1;
          _hlon[k] = lon;
          _hlat[k] = lat;
          _hx[k] = x0;
          _hy[k] = y0;
          continue;
        }
        _plon[i] = _ll[0];
        _plat[i] = _ll[1];
        _age[i]++;
        _hlon[k] = _ll[0];
        _hlat[k] = _ll[1];
        _hx[k] = x1;
        _hy[k] = y1;
        if (_hcount[i] < _histLen) _hcount[i]++;
      }
      if (!sameView) _reprojectHistory(s, skip: _head);
    }
    _workMs += sw.elapsedMicroseconds / 1000.0;
    _frameCount++;
    final now = _clock.elapsedMilliseconds / 1000.0;
    if (now - _lastReport >= 2.0) {
      final fps = _frameCount / (now - _lastReport);
      widget.control.triggerEvent("stats", {
        "fps": fps,
        "frame_ms": _frameCount > 0 ? _workMs / _frameCount : 0.0,
        "particles": _n,
        "upload_ms": _uploadMs,
        "build_ms": _timingFrames > 0 ? _buildMs / _timingFrames : 0.0,
        "raster_ms": _timingFrames > 0 ? _rasterMs / _timingFrames : 0.0,
        "frames": _frames.length,
      });
      _frameCount = 0;
      _workMs = 0;
      _lastReport = now;
      _timingFrames = 0;
      _buildMs = 0;
      _rasterMs = 0;
    }
    _repaint.value++;
  }

  /// 表示が変わったとき、輪の画面の位置を経緯度から計算し直す。
  void _reprojectHistory(Size s, {int skip = -1}) {
    final grid = _grid, proj = _proj;
    if (grid == null || proj == null) return;
    for (var i = 0; i < _n; i++) {
      final cnt = _hcount[i];
      for (var a = 0; a < cnt; a++) {
        final slot = (_head - a + _histLen) % _histLen;
        if (slot == skip) continue;
        final k = i * _histLen + slot;
        proj.toScreen(_hlon[k], _hlat[k], s, grid.periodic, _xy);
        _hx[k] = _xy[0];
        _hy[k] = _xy[1];
      }
    }
    _histViewKey = proj.key(s);
  }

  /// int16（0.01 m/s）の格子の双一次補間。
  static double _sample16(_Grid g, double fi, double fj, Int16List a) {
    var i0 = fi.floor();
    var j0 = fj.floor();
    if (j0 > g.ny - 2) j0 = g.ny - 2;
    int i1;
    double fx;
    if (g.periodic) {
      fx = fi - i0;
      i0 = i0 % g.nx;
      i1 = (i0 + 1) % g.nx;
    } else {
      if (i0 > g.nx - 2) i0 = g.nx - 2;
      i1 = i0 + 1;
      fx = fi - i0;
    }
    final fy = fj - j0;
    final r0 = j0 * g.nx, r1 = r0 + g.nx;
    return ((a[r0 + i0] * (1 - fx) + a[r0 + i1] * fx) * (1 - fy) +
            (a[r1 + i0] * (1 - fx) + a[r1 + i1] * fx) * fy) *
        0.01;
  }

  /// 尾を三角形の列にして 1 回で描く。新しい区間ほど濃く。
  void _paintParticles(Canvas canvas, Size s) {
    final grid = _grid, proj = _proj;
    if (grid == null || proj == null) return;
    if (_histViewKey != proj.key(s)) _reprojectHistory(s);
    const hw = 0.6;
    final jump = s.width / 2;
    var p = 0, c = 0;
    for (var i = 0; i < _n; i++) {
      final cnt = _hcount[i];
      if (cnt < 2) continue;
      final b = i * _histLen;
      for (var a = 0; a < cnt - 1; a++) {
        final kNew = b + (_head - a + _histLen) % _histLen;
        final kOld = b + (_head - a - 1 + _histLen) % _histLen;
        final x0 = _hx[kOld], y0 = _hy[kOld], x1 = _hx[kNew], y1 = _hy[kNew];
        final dx = x1 - x0, dy = y1 - y0;
        if (dx.abs() > jump) continue; // 経度のつなぎ目をまたいだ区間
        final len = math.sqrt(dx * dx + dy * dy);
        if (len < 1e-3) continue;
        final nx = -dy / len * hw, ny = dx / len * hw;
        _vpos[p++] = x0 + nx;
        _vpos[p++] = y0 + ny;
        _vpos[p++] = x0 - nx;
        _vpos[p++] = y0 - ny;
        _vpos[p++] = x1 + nx;
        _vpos[p++] = y1 + ny;
        _vpos[p++] = x1 + nx;
        _vpos[p++] = y1 + ny;
        _vpos[p++] = x0 - nx;
        _vpos[p++] = y0 - ny;
        _vpos[p++] = x1 - nx;
        _vpos[p++] = y1 - ny;
        final aNew = (230 * (1 - a / (_histLen - 1))).round();
        final aOld = (230 * (1 - (a + 1) / (_histLen - 1))).round();
        final cNew = (aNew << 24) | 0xFFFFFF, cOld = (aOld << 24) | 0xFFFFFF;
        _vcol[c++] = cOld;
        _vcol[c++] = cOld;
        _vcol[c++] = cNew;
        _vcol[c++] = cNew;
        _vcol[c++] = cOld;
        _vcol[c++] = cNew;
      }
    }
    if (c == 0) return;
    final vertices = ui.Vertices.raw(ui.VertexMode.triangles,
        Float32List.sublistView(_vpos, 0, p),
        colors: Int32List.sublistView(_vcol, 0, c));
    canvas.drawVertices(vertices, BlendMode.dst, Paint());
    vertices.dispose();
  }

  /// 経緯度の線（NaN で切れ目）を画面の Path に。経度のつなぎ目をまたぐ区間は切る。
  Path _path(Float32List pts, Size s) {
    final grid = _grid, proj = _proj!;
    final periodic = grid?.periodic ?? false;
    final p = Path();
    var pen = false;
    double px = 0;
    final jump = s.width / 2;
    for (var k = 0; k + 1 < pts.length; k += 2) {
      final lon = pts[k], lat = pts[k + 1];
      if (lon.isNaN || lat.isNaN) {
        pen = false;
        continue;
      }
      proj.toScreen(lon, lat, s, periodic, _xy);
      if (pen && (_xy[0] - px).abs() > jump) pen = false;
      if (pen) {
        p.lineTo(_xy[0], _xy[1]);
      } else {
        p.moveTo(_xy[0], _xy[1]);
        pen = true;
      }
      px = _xy[0];
    }
    return p;
  }

  Path? _coastPathFor(Size s) {
    final coast = _coast;
    if (coast == null || _proj == null) return null;
    final key = _proj!.key(s);
    if (_coastPath != null && key == _coastKey) return _coastPath;
    _coastPath = _path(coast, s);
    _coastKey = key;
    return _coastPath;
  }

  void _ensureContours(_Frame f, int idx, Size s) {
    final key = "$idx,${_proj!.key(s)}";
    if (_contourPath != null && key == _contourKey) return;
    _contourPath = f.contours != null ? _path(f.contours!, s) : null;
    _boldPath = f.bold != null ? _path(f.bold!, s) : null;
    _contourKey = key;
  }

  /// 海岸線・等値線・数値を描く。
  void _paintVector(Canvas canvas, Size size, _Frame? frame, int idx) {
    final proj = _proj, grid = _grid;
    final coast = _coastPathFor(size);
    if (coast != null) {
      canvas.drawPath(
          coast,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.0
            ..color = const Color(0xE6050508));
    }
    if (frame == null || proj == null) return;
    _ensureContours(frame, idx, size);
    final thin = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0
      ..color = const Color(0xE6FFFFFF);
    if (_contourPath != null) canvas.drawPath(_contourPath!, thin);
    if (_boldPath != null) canvas.drawPath(_boldPath!, thin..strokeWidth = 2.0);
    final labels = frame.labels;
    if (labels != null) {
      for (var k = 0; k + 2 < labels.length; k += 3) {
        proj.toScreen(labels[k], labels[k + 1], size, grid?.periodic ?? false, _xy);
        final x = _xy[0], y = _xy[1];
        if (x < 10 || y < 10 || x > size.width - 10 || y > size.height - 10) continue;
        _Painter._pill(canvas, labels[k + 2].round().toString(), Offset(x, y));
      }
    }
  }

  ui.Image? _vectorImage;
  String _vectorKey = "";
  double _dpr = 1.0;
  bool _interacting = false;
  Timer? _interactTimer;

  /// 静止中の海岸線・等値線・数値の画像（画面の画素密度で作る。ぼけないように）。
  ui.Image? _vectorImageFor(Size size, _Frame? frame, int idx) {
    final proj = _proj;
    if (proj == null) return null;
    final key = "${proj.key(size)}|$idx|${identityHashCode(frame)}|${identityHashCode(_coast)}|$_dpr";
    if (_vectorImage != null && key == _vectorKey) return _vectorImage;
    final rec = ui.PictureRecorder();
    final canvas = Canvas(rec);
    canvas.scale(_dpr);
    _paintVector(canvas, size, frame, idx);
    final img = rec.endRecording().toImageSync((size.width * _dpr).ceil(), (size.height * _dpr).ceil());
    _vectorImage?.dispose();
    _vectorImage = img;
    _vectorKey = key;
    return img;
  }

  /// 拡大・移動の間は線を直接描き、手を止めて少ししたら画像に戻す。
  void _markInteracting() {
    _interacting = true;
    _interactTimer?.cancel();
    _interactTimer = Timer(const Duration(milliseconds: 250), () {
      if (!mounted) return;
      _interacting = false;
      _repaint.value++;
    });
  }

  void _viewChanged({bool respawn = false}) {
    _coastPath = null;
    _contourPath = null;
    if (respawn && _n > 0) _initParticles(_n);
    _repaint.value++;
  }

  void _onScale(ScaleUpdateDetails d) {
    final proj = _proj;
    if (proj == null) return;
    if (d.pointerCount >= 2 && d.scale != _scaleBase && _scaleBase > 0) {
      proj.zoomAt(d.localFocalPoint, d.scale / _scaleBase, _size);
      _scaleBase = d.scale;
    }
    if (d.focalPointDelta != Offset.zero) proj.pan(d.focalPointDelta);
    _markInteracting();
    _viewChanged();
  }

  @override
  Widget build(BuildContext context) {
    _dpr = MediaQuery.devicePixelRatioOf(context);
    _sync();
    return LayoutControl(
      control: widget.control,
      child: LayoutBuilder(builder: (context, cons) {
        final size = Size(cons.maxWidth.isFinite ? cons.maxWidth : 800,
            cons.maxHeight.isFinite ? cons.maxHeight : 600);
        if (size != _size) {
          _size = size;
          _viewChanged(respawn: true);
        }
        return Listener(
          onPointerSignal: (e) {
            if (e is PointerScrollEvent && _proj != null) {
              _proj!.zoomAt(e.localPosition, math.exp(-e.scrollDelta.dy / 300), _size);
              _markInteracting();
              _viewChanged();
            }
          },
          child: GestureDetector(
            onScaleStart: (_) => _scaleBase = 1.0,
            onScaleUpdate: _onScale,
            child: MouseRegion(
              onHover: (e) => _hover = e.localPosition,
              onExit: (_) => _hover = null,
              child: ClipRect(child: CustomPaint(size: size, painter: _Painter(this))),
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
    final c = s.widget.control;
    final rect = Offset.zero & size;
    final shader = s._shader, lutImg = s._lutImage, grid = s._grid, proj = s._proj;
    final idx = s._current;
    final tex = s._textures[idx];
    final frame = s._frames[idx];
    if (shader != null && lutImg != null && grid != null && proj != null && tex != null) {
      final base = s._baseImage;
      shader
        ..setFloat(0, size.width)
        ..setFloat(1, size.height)
        ..setFloat(6, grid.lon0)
        ..setFloat(7, grid.lat0)
        ..setFloat(8, grid.dlon)
        ..setFloat(9, grid.dlat)
        ..setFloat(10, grid.nx.toDouble())
        ..setFloat(11, grid.ny.toDouble())
        ..setFloat(12, proj.mode.toDouble())
        ..setFloat(13, grid.periodic ? 1 : 0)
        ..setFloat(14, base != null ? 1 : 0)
        ..setFloat(15, s._nearest ? 1 : 0)
        ..setImageSampler(0, tex)
        ..setImageSampler(1, lutImg)
        ..setImageSampler(2, base ?? lutImg, filterQuality: FilterQuality.low);
      proj.setUniform(shader, size);
      canvas.drawRect(rect, Paint()..shader = shader);
    } else {
      canvas.drawRect(rect, Paint()..color = const Color(0xFF1A1C21));
      final why = s._shaderError.isNotEmpty
          ? "色の場を描けません（シェーダー）: ${s._shaderError}"
          : (shader == null ? "シェーダーを読み込み中…" : (lutImg == null ? "配色を読み込み中…" : ""));
      if (why.isNotEmpty) _label(canvas, why, Offset(10, size.height - 30));
    }

    // 海岸線・等値線・数値は、表示も時刻も変わらない間は同じ絵なので画像にして使い回す
    // （毎コマ描くと、線を三角形に分ける手間で 1 コマ十数ミリ秒かかっていた）。
    // 拡大・移動の最中は画像を作り直さず、直接描く
    if (s._interacting) {
      s._paintVector(canvas, size, frame, idx);
    } else {
      final img = s._vectorImageFor(size, frame, idx);
      if (img != null) {
        canvas.drawImageRect(img, Offset.zero & Size(img.width.toDouble(), img.height.toDouble()),
            rect, Paint()..filterQuality = FilterQuality.medium);
      }
    }

    if (s._showParticles && s._n > 0 && frame?.u != null) s._paintParticles(canvas, size);

    // 読み取り
    final h = s._hover;
    if (h != null && grid != null && proj != null && frame != null) {
      if (proj.toLonLat(h.dx, h.dy, size, s._ll) && grid.locate(s._ll[0], s._ll[1], s._ij)) {
        final vmin = c.getDouble("vmin", 0.0)!, vmax = c.getDouble("vmax", 1.0)!;
        final q = s._nearest
            ? frame.q(_nearestIndex(grid, s._ij[0], s._ij[1]))
            : _bilinear(grid, s._ij[0], s._ij[1], frame.q);
        final label = c.getString("label", "")!;
        final unit = c.getString("unit", "")!;
        final parts = <String>[_lonlat(s._ll[0], s._ll[1])];
        if (!q.isNaN) {
          final v = vmin + q * (vmax - vmin);
          if (s._classes.isEmpty) {
            parts.add("$label ${v.toStringAsFixed(1)}$unit");
          } else {
            final i = v.round();
            final name = i >= 0 && i < s._classes.length ? s._classes[i] : "";
            if (name.isNotEmpty) {
              parts.add([label, name, unit].where((x) => x.isNotEmpty).join(" "));
            }
          }
        }
        if (frame.u != null) {
          final u = _bilinear(grid, s._ij[0], s._ij[1], (k) => frame.u![k] * 0.01);
          final v = _bilinear(grid, s._ij[0], s._ij[1], (k) => frame.v![k] * 0.01);
          parts.add("風 ${math.sqrt(u * u + v * v).toStringAsFixed(1)} m/s");
        }
        _label(canvas, parts.join("  "), h + const Offset(14, 14));
      }
    }

    // 状態の知らせ（読み込み中、この時刻のデータが無い、など）は Python 側が決める
    final status = c.getString("status_text", "")!;
    if (status.isNotEmpty) _label(canvas, status, const Offset(10, 10));
    final attribution = c.getString("attribution", "")!;
    if (attribution.isNotEmpty) {
      final tp = _painter(attribution, 11, const Color(0xFFF0F0F0));
      _label(canvas, attribution,
          Offset(size.width - tp.width - 14, size.height - tp.height - 10),
          fontSize: 11);
    }
  }

  static String _lonlat(double lon, double lat) {
    var l = ((lon + 180) % 360 + 360) % 360 - 180;
    final ew = l >= 0 ? "E" : "W";
    final ns = lat >= 0 ? "N" : "S";
    return "${lat.abs().toStringAsFixed(2)}°$ns ${l.abs().toStringAsFixed(2)}°$ew";
  }

  static TextPainter _painter(String text, double fontSize, Color color) => TextPainter(
        text: TextSpan(text: text, style: TextStyle(color: color, fontSize: fontSize)),
        textDirection: TextDirection.ltr,
      )..layout();

  static void _label(Canvas canvas, String text, Offset at, {double fontSize = 12}) {
    final tp = _painter(text, fontSize, const Color(0xFFF0F0F0));
    canvas.drawRRect(
        RRect.fromRectAndRadius(
            Rect.fromLTWH(at.dx - 4, at.dy - 2, tp.width + 8, tp.height + 4),
            const Radius.circular(4)),
        Paint()..color = const Color(0xA0000000));
    tp.paint(canvas, at);
  }

  /// 等値線の数値（線の上に置く丸い札）。
  static void _pill(Canvas canvas, String text, Offset center) {
    final tp = _painter(text, 10, const Color(0xFFFFFFFF));
    final w = tp.width + 8, h = tp.height + 2;
    canvas.drawRRect(
        RRect.fromRectAndRadius(
            Rect.fromCenter(center: center, width: w, height: h), Radius.circular(h / 2)),
        Paint()..color = const Color(0xB0303846));
    tp.paint(canvas, center - Offset(tp.width / 2, tp.height / 2));
  }

  @override
  bool shouldRepaint(covariant _Painter oldDelegate) => true;
}
