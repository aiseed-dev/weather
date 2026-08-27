// 格子を1枚の四角形に貼り、値から色をLUTで引く。
//
// **色付けをGPUでやるのが要点。** Python側は値のまま渡すので、
// パン・ズームで色を作り直す必要がない(境界を越えない)。
// 欠測(NaN)は透明にする。

struct View {
    // 表示している経緯度の範囲。パン・ズームはこれを書き換えるだけ
    center: vec2<f32>,
    scale:  vec2<f32>,
    vmin:   f32,
    vmax:   f32,
};

@group(0) @binding(0) var<uniform> view: View;
@group(0) @binding(1) var grid: texture_2d<f32>;
@group(0) @binding(2) var grid_s: sampler;
@group(0) @binding(3) var lut: texture_1d<f32>;
@group(0) @binding(4) var lut_s: sampler;

struct VsOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@vertex
fn vs(@builtin(vertex_index) i: u32) -> VsOut {
    // 画面いっぱいの三角形2枚(頂点バッファを持たない)
    var p = array<vec2<f32>, 6>(
        vec2(-1.0, -1.0), vec2(1.0, -1.0), vec2(-1.0, 1.0),
        vec2(-1.0,  1.0), vec2(1.0, -1.0), vec2( 1.0, 1.0));
    let xy = p[i];
    var o: VsOut;
    o.pos = vec4(xy, 0.0, 1.0);
    // 画面(-1..1) → テクスチャ(0..1)。中心と倍率で見る範囲を決める
    o.uv = view.center + vec2(xy.x, -xy.y) * view.scale;
    return o;
}

@fragment
fn fs(in: VsOut) -> @location(0) vec4<f32> {
    if (in.uv.x < 0.0 || in.uv.x > 1.0 || in.uv.y < 0.0 || in.uv.y > 1.0) {
        return vec4(0.10, 0.13, 0.16, 1.0);      // 枠外
    }
    let v = textureSample(grid, grid_s, in.uv).r;
    if (v != v) {                                 // NaN = 欠測
        return vec4(0.10, 0.13, 0.16, 1.0);
    }
    let t = clamp((v - view.vmin) / (view.vmax - view.vmin), 0.0, 1.0);
    return vec4(textureSample(lut, lut_s, t).rgb, 1.0);
}
