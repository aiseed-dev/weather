/**
 * アメダス地点別データの取得（子）。**1 エリア分を 1 ファイルにまとめて** R2 へ置く。
 *
 * ステートレスにしてあるのが要点。担当エリアを引数で受けるので、同じスクリプトを
 * 何度でも並列に呼べる（エリア数だけデプロイする必要がない）。
 *
 * なぜ地点ごとではなくエリアごとに置くか
 * --------------------------------------
 * 1. **サブリクエストは 50/実行**。R2 の put もこれに数えられる
 *    （"A subrequest is any request a Worker makes using the Fetch API or to
 *    Cloudflare services like R2, KV, or D1."）。地点ごとに置くと
 *    fetch N + put N = 2N になり、25 地点で頭打ちになる。
 *    まとめれば fetch N + put 1 で、最大エリア 47 地点でも 48 で収まる。
 * 2. **R2 の Class A（書き込み）は月 100 万回**。進行中の 3 時間ブロックは
 *    10 分ごとに上書きされるので、地点ごとだと 1,286 × 144 × 30 ＝ 月 556 万回で
 *    無料枠を大きく超える。エリア単位なら 64 × 144 × 30 ＝ 月 27.6 万回。
 * 3. **読み方に合う**。気象庁の「◯◯県の観測データ」のように、県内の全地点を
 *    一覧するのが主な使い方。その画面がファイル 1 本で作れる。
 *
 * 本文は復号しない
 * ----------------
 * CPU は 10ms しかないので JSON.parse はしない。気象庁のペイロードを
 * バイト列のまま `{"11001":<生>,"16001":<生>}` と連結するだけにする。
 * 文字列に直すと UTF-8 の復号と再符号化で 2 往復ぶん余計に食う。
 *
 * R2 レイアウト:
 *   point/{YYYYMMDD}/{HH}/{area_code}.json
 *
 * **日付を最上位に置く。** 半月分を NetCDF へ封入したあと生 JSON を消す運用で、
 * 日付が上なら 1 階層の削除で済む。
 *
 * **3 時間ブロック（HH = 00/03/…/21）もキーに残す。** 日単位のキーに書くと、
 * 15 時台の取得が 12 時台までの内容を消してしまう。まとめて 1 本にするには
 * 既存を読んで併合することになるが、190KB の JSON.parse は CPU 10ms に
 * 載らない。ブロックごとに分けておけば、上書きだけで正しく貯まる。
 *
 * 大きさ（実測 2026-08-29 / 東京 44132）: 1 スロット約 528 バイト × 18 スロット
 * ＝ 1 地点 1 ブロック約 9.5KB。エリア 1 本は平均 190KB・最大 450KB。
 */

const JMA = "https://www.jma.go.jp/bosai/amedas/data/point";
const UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)";
const CONCURRENCY = 6;      // Workers の同時接続上限に合わせる

const enc = new TextEncoder();

/** 1 地点の 3 時間ブロックを取る。本文はバイト列のまま返す。 */
async function fetchOne(code, day, hour) {
  const res = await fetch(`${JMA}/${code}/${day}_${hour}.json`, {
    headers: { "User-Agent": UA },
  });
  // 404 は正常に起こりうる（そのブロックがまだ無い／その地点が休止中）
  if (res.status === 404) return { code, state: "missing" };
  if (!res.ok) return { code, state: "error" };
  return { code, state: "stored", body: new Uint8Array(await res.arrayBuffer()) };
}

/** 取れた地点を 1 つの JSON オブジェクトに連結する（復号しない）。 */
function combine(parts) {
  const chunks = [];
  let size = 0;
  const push = (u8) => { chunks.push(u8); size += u8.length; };

  push(enc.encode("{"));
  let first = true;
  for (const p of parts) {
    if (p.state !== "stored") continue;
    push(enc.encode(`${first ? "" : ","}"${p.code}":`));
    push(p.body);
    first = false;
  }
  push(enc.encode("}"));

  const out = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

async function handle(env, area, stations, day, hour) {
  const parts = new Array(stations.length);
  let i = 0;
  const worker = async () => {
    while (i < stations.length) {
      const n = i++;
      try {
        parts[n] = await fetchOne(stations[n], day, hour);
      } catch (_) {
        parts[n] = { code: stations[n], state: "error" };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, stations.length) },
                               worker));

  const tally = { stored: 0, missing: 0, error: 0 };
  for (const p of parts) tally[p.state]++;

  // 1 地点も取れなければ置かない。空の {} で上書きすると、直前まで取れていた
  // 内容を消してしまう（気象庁側の一時的な不調で全滅することがある）
  if (tally.stored === 0) return { area, ...tally, written: 0 };

  await env.AMEDAS.put(`point/${day}/${hour}/${area}.json`, combine(parts), {
    httpMetadata: {
      contentType: "application/json; charset=utf-8",
      // 進行中のブロックは上書きされ続けるので短命にする
      cacheControl: "public, max-age=600",
    },
  });
  return { area, ...tally, written: 1 };
}

export default {
  /**
   * 親からの呼び出し口。
   *   POST /  {"day":"20260829","hour":"12",
   *            "area":"011000","stations":["11001","11016", ...]}
   */
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
    const { area, stations, day, hour } = body || {};
    if (!area || !Array.isArray(stations) || !stations.length
        || !/^\d{8}$/.test(day || "") || !/^\d{2}$/.test(hour || "")) {
      return new Response("area と stations(配列) と day(YYYYMMDD) と hour(HH) が要る",
                          { status: 400 });
    }
    // fetch は地点数、put は 1。50 を超える依頼は受けない（黙って欠けるより良い）
    if (stations.length + 1 > 50) {
      return new Response(`地点が多すぎます（${stations.length}）。`
                          + "サブリクエストは 1 実行 50 件まで", { status: 400 });
    }
    return Response.json({ ok: 1, ...await handle(env, area, stations, day, hour) });
  },
};
