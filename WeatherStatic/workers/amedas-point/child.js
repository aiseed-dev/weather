/**
 * アメダス地点別データの取得（子）。親から地点リストを受け取り、取得して R2 へ置く。
 *
 * ステートレスにしてあるのが要点。担当地点を引数で受けるので、**同じスクリプトを
 * 何度でも並列に呼べる**（子を地点数だけデプロイする必要がない）。
 *
 * 取得作法:
 *   - 気象庁のペイロードは**加工せずそのまま**置く。無料枠の CPU は 10ms しか
 *     なく、JSON.parse する余裕がない。加工は tgsvr が R2 から拾って行う
 *   - 同時に開ける接続は 6 が上限なので、CONCURRENCY で明示的に絞る
 *   - 404 は正常に起こりうる（その 3 時間ブロックがまだ無い等）。再試行しない
 *
 * R2 レイアウト:
 *   point/{YYYYMMDD}/{area_code}/{アメダス番号}.json
 *
 * **日付を最上位に置く。** 半月分を NetCDF へ封入したあと生 JSON を消す運用で、
 * 日付が上なら 1 階層の削除で済む。area_code（気象庁の予報区。北海道 8・
 * 鹿児島 2・沖縄 4 に分かれる）で分けるのは、1 ディレクトリ 8〜47 地点に
 * 収まり、地域の障害を切り分けられるため。
 *
 * 3 時間ファイル（{YYYYMMDD}_{HH}.json）は 1 日 8 本あるが、**同じ日の分は
 * 1 本にまとめて置く**。日をまたがない限り上書きで済み、日別の扱いが単純になる。
 */

const JMA = "https://www.jma.go.jp/bosai/amedas/data/point";
const UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)";
const CONCURRENCY = 6;      // Workers の同時接続上限に合わせる

async function fetchOne(env, st, day, hour) {
  // st = [アメダス番号, area_code]
  const [code, area] = st;
  const key = `point/${day}/${area}/${code}.json`;
  const res = await fetch(`${JMA}/${code}/${day}_${hour}.json`, {
    headers: { "User-Agent": UA },
  });
  if (!res.ok) return res.status === 404 ? "missing" : "error";
  // 本文に触らずストリームのまま流す（CPU 10ms の制約）
  await env.AMEDAS.put(key, res.body, {
    httpMetadata: {
      contentType: "application/json; charset=utf-8",
      // 進行中のブロックは上書きされ続けるので短命にする
      cacheControl: "public, max-age=600",
    },
  });
  return "stored";
}

async function handle(env, stations, day, hour) {
  const tally = { stored: 0, missing: 0, error: 0 };
  let i = 0;
  const worker = async () => {
    while (i < stations.length) {
      const st = stations[i++];
      try {
        tally[await fetchOne(env, st, day, hour)]++;
      } catch (_) {
        tally.error++;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, stations.length) },
                               worker));
  return tally;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    let body;
    try {
      body = await request.json();
    } catch (_) {
      return new Response("bad request", { status: 400 });
    }
    const { stations, day, hour } = body || {};
    if (!Array.isArray(stations) || !/^\d{8}$/.test(day || "")
        || !/^\d{2}$/.test(hour || "")) {
      return new Response("stations(配列) と day(YYYYMMDD) と hour(HH) が要る",
                          { status: 400 });
    }
    const t = await handle(env, stations, day, hour);
    return Response.json({ ok: 1, ...t, failed: t.error });
  },
};
