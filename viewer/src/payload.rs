//! Python が書き出した 1 ステップ分を読む(AWGP 形式)。
//!
//! 画像ではなく**値の格子**を受け取る。色付けはシェーダが LUT を引いて
//! 行うので、値そのものが手元に残り、カーソル位置の実数値も読める。
//! 形式の定義は src/aiseed_weather/figures/gpu_handoff.py と対。

use std::path::Path;

pub struct Payload {
    pub nx: usize,
    pub ny: usize,
    pub vmin: f32,
    pub vmax: f32,
    pub data: Vec<f32>,   // ny * nx。行 0 が北端
    pub lons: Vec<f32>,
    pub lats: Vec<f32>,
    pub lut: Vec<u8>,     // 256 * 3
    pub meta: String,     // ヘッダの JSON そのまま(表示用)
}

fn u32_at(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

/// JSON から数値を 1 つ取る。ヘッダは自分で書いた小さな物なので、
/// 解析器を足すより読む場所を限る方が依存が減る。
fn num(json: &str, key: &str) -> Option<f64> {
    let pat = format!("\"{key}\":");
    let i = json.find(&pat)? + pat.len();
    let rest = &json[i..];
    let end = rest.find(|c: char| c == ',' || c == '}')?;
    rest[..end].trim().parse().ok()
}

impl Payload {
    pub fn load(path: &Path) -> Result<Self, String> {
        let b = std::fs::read(path).map_err(|e| format!("読めない: {e}"))?;
        if b.len() < 12 || &b[..4] != b"AWGP" {
            return Err("形式が違う(AWGP ではない)".into());
        }
        let version = u32_at(&b, 4);
        if version != 1 {
            return Err(format!("版が違う: {version}"));
        }
        let hlen = u32_at(&b, 8) as usize;
        let meta = String::from_utf8_lossy(&b[12..12 + hlen]).into_owned();
        let nx = num(&meta, "nx").ok_or("nx が無い")? as usize;
        let ny = num(&meta, "ny").ok_or("ny が無い")? as usize;
        let vmin = num(&meta, "vmin").ok_or("vmin が無い")? as f32;
        let vmax = num(&meta, "vmax").ok_or("vmax が無い")? as f32;

        let mut off = 12 + hlen;
        let take_f32 = |off: &mut usize, n: usize| -> Vec<f32> {
            let v = (0..n).map(|i| f32::from_le_bytes([
                b[*off + i * 4], b[*off + i * 4 + 1],
                b[*off + i * 4 + 2], b[*off + i * 4 + 3]])).collect();
            *off += n * 4;
            v
        };
        let data = take_f32(&mut off, ny * nx);
        let lons = take_f32(&mut off, nx);
        let lats = take_f32(&mut off, ny);
        let lut = b[off..off + 256 * 3].to_vec();

        Ok(Self { nx, ny, vmin, vmax, data, lons, lats, lut, meta })
    }

    /// 画面座標(0..1)から値を引く。カーソル位置の実数値を出すため。
    pub fn value_at(&self, u: f32, v: f32) -> Option<f32> {
        if !(0.0..1.0).contains(&u) || !(0.0..1.0).contains(&v) {
            return None;
        }
        let x = (u * self.nx as f32) as usize;
        let y = (v * self.ny as f32) as usize;
        let val = *self.data.get(y * self.nx + x)?;
        if val.is_nan() { None } else { Some(val) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Python が書いた本物のペイロードを読めるか。形式の対応が崩れると
    /// 画面が出てから気づくことになるので、ここで壁にする。
    #[test]
    fn reads_python_payload() {
        let p = Path::new("/tmp/msl.awgp");
        if !p.exists() {
            eprintln!("飛ばす: {p:?} が無い（先に gpu_handoff.write_payload で作る）");
            return;
        }
        let pl = Payload::load(p).expect("読めること");
        assert_eq!((pl.nx, pl.ny), (1440, 721), "格子の大きさ");
        assert_eq!(pl.data.len(), 1440 * 721, "値の数");
        assert_eq!(pl.lut.len(), 256 * 3, "LUT の大きさ");
        assert_eq!(pl.lons.len(), pl.nx);
        assert_eq!(pl.lats.len(), pl.ny);
        // 行 0 が北端(_fast.crop_grid の約束)
        assert!(pl.lats[0] > pl.lats[pl.ny - 1], "行0が北端であること");
        // 値が値域に収まっている
        let finite: Vec<f32> = pl.data.iter().copied().filter(|v| v.is_finite()).collect();
        let lo = finite.iter().cloned().fold(f32::MAX, f32::min);
        let hi = finite.iter().cloned().fold(f32::MIN, f32::max);
        assert!(lo >= pl.vmin - 1.0 && hi <= pl.vmax + 1.0,
                "値 {lo}..{hi} が値域 {}..{} に収まる", pl.vmin, pl.vmax);
        // 値の読み取り(カーソル位置の実数値)
        let v = pl.value_at(0.5, 0.5).expect("中央の値");
        assert!(v.is_finite());
        println!("読めた: {}x{} 値域 {:.0}..{:.0} 実測 {lo:.0}..{hi:.0} 中央 {v:.0}",
                 pl.nx, pl.ny, pl.vmin, pl.vmax);
    }
}
