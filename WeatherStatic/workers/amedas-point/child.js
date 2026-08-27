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
 *   point/{アメダス番号}/{YYYYMMDD_HH}.json … 生ペイロード（3 時間分の 10 分値）
 */

const JMA = "https://www.jma.go.jp/bosai/amedas/data/point";
const UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)";
const CONCURRENCY = 6;      // Workers の同時接続上限に合わせる

async function fetchOne(env, code, block) {
  const key = `point/${code}/${block}.json`;
  const res = await fetch(`${JMA}/${code}/${block}.json`, {
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

async function handle(env, stations, block) {
  const tally = { stored: 0, missing: 0, error: 0 };
  let i = 0;
  const worker = async () => {
    while (i < stations.length) {
      const code = stations[i++];
      try {
        tally[await fetchOne(env, code, block)]++;
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
    const { stations, block } = body || {};
    if (!Array.isArray(stations) || !block) {
      return new Response("stations と block が要る", { status: 400 });
    }
    const t = await handle(env, stations, block);
    return Response.json({ ok: 1, ...t, failed: t.error });
  },
};
