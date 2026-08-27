/**
 * アメダス 10 分値の収集 (Cloudflare Workers + R2)。
 *
 * 気象庁の map JSON は **約 10 日で消える**（実測: 10 日前 200 / 11 日前 404）。
 * 取り逃した分は二度と取れないため、収集は自宅サーバーの死活から切り離す。
 * この Worker が 10 分ごとに取得して R2 へ置き、tgsvr はそこから拾って
 * NetCDF アーカイブへ封入する（重い処理は Workers の CPU/メモリに載らない）。
 *
 * 役割分担（ここを踏み外さないこと）:
 *   Worker … 取得して **そのまま** R2 に置く。それだけ。
 *   tgsvr  … R2 から拾って NetCDF へ封入し、observations.nc を維持する。
 *            netCDF4 / numpy を使う処理はすべて向こう側。
 *
 * 設計の要点:
 *   - **配信はしない**。R2 に置くだけ。Worker を公開エンドポイントにすると
 *     無料枠の 10 万リクエスト/日を訪問者が食い潰すため、読み出しは R2 に任せる
 *   - **加工もパースもしない**。ReadableStream のまま R2 へ流す。無料枠の
 *     CPU は 10ms しかなく、245KB の JSON.parse に使う余裕はない。
 *     生のまま置いておけば、後から解釈を変えたくなっても作り直せる
 *   - **取りこぼしを自力で埋める**。前回落ちていても、窓の中の未取得スロットを
 *     新しい順に拾う。JMA への配慮で 1 回の実行あたり MAX_PER_RUN 件まで
 *
 * R2 レイアウト（fetch_amedas_mirror.py が作るものと同じ形）:
 *   map/{YYYYMMDDHHMM00}.json … 生ペイロード（不変）
 *   latest.json               … 最新スロットの複製
 *   latest_time.txt           … 最新スロットの時刻
 *
 * デプロイ（ユーザー実行）:
 *   1. R2 バケット "weather-amedas" を作成
 *   2. wrangler deploy（wrangler.toml に cron と R2 binding が入っている）
 *   3. 公開するなら R2 バケットに独自ドメインを割り当てる（例 amedas.time-j.net）
 */

const JMA = "https://www.jma.go.jp/bosai/amedas/data";
const UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)";
const SLOT_MS = 10 * 60 * 1000;
// 気象庁の保持期間に合わせる。これより古いスロットは取りに行っても 404
const WINDOW_SLOTS = 10 * 24 * 6;
// 1 回の実行で取りに行く上限。長時間停止していても一気に叩かない
const MAX_PER_RUN = 12;
const GAP_MS = 300;

const pad = (n, w = 2) => String(n).padStart(w, "0");

/** JST の Date をスロット名 YYYYMMDDHHMM00 にする */
function slotName(d) {
  return d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate())
       + pad(d.getUTCHours()) + pad(d.getUTCMinutes()) + "00";
}

/** latest_time.txt（JST の ISO 文字列）を 10 分スロットに丸めた「JST の壁時計」を返す */
async function latestSlot() {
  const res = await fetch(`${JMA}/latest_time.txt`, { headers: { "User-Agent": UA } });
  if (!res.ok) throw new Error(`latest_time.txt: ${res.status}`);
  const raw = (await res.text()).trim();
  // 例 2026-08-27T13:10:00+09:00。時差計算を避けるため、壁時計をそのまま UTC として持つ
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) throw new Error(`latest_time.txt が読めない: ${raw}`);
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
  d.setUTCMinutes(Math.floor(d.getUTCMinutes() / 10) * 10, 0, 0);
  return { slot: d, raw };
}

/** 1 スロットを取得して R2 へ置く。既にあれば触らない。置けたら true */
async function storeSlot(env, name) {
  if (await env.AMEDAS.head(`map/${name}.json`)) return false;
  const res = await fetch(`${JMA}/map/${name}.json`, { headers: { "User-Agent": UA } });
  if (!res.ok) return false;                       // 欠番スロットは正常に起こりうる
  // 本文には触らない。ストリームのまま渡して CPU を使わない
  await env.AMEDAS.put(`map/${name}.json`, res.body, {
    httpMetadata: {
      contentType: "application/json; charset=utf-8",
      cacheControl: "public, max-age=31536000, immutable",
    },
  });
  return true;
}

async function collect(env) {
  const { slot, raw } = await latestSlot();
  let fetched = 0;
  let budget = MAX_PER_RUN;

  for (let i = 0; i < WINDOW_SLOTS && budget > 0; i++) {
    const name = slotName(new Date(slot.getTime() - i * SLOT_MS));
    if (await env.AMEDAS.head(`map/${name}.json`)) {
      // 直近が既にあるなら、その先も埋まっているとみなして打ち切る。
      // 穴が空いたままなら次回以降の実行で i が伸びて到達する
      if (i === 0) continue;
      break;
    }
    budget--;
    if (await storeSlot(env, name)) fetched++;
    if (budget > 0) await new Promise(r => setTimeout(r, GAP_MS));
  }

  // 最新スロットの複製と時刻。消費側がベース URL を差し替えるだけで済むようにする
  const newest = slotName(slot);
  const head = await env.AMEDAS.head(`map/${newest}.json`);
  if (head) {
    const obj = await env.AMEDAS.get(`map/${newest}.json`);
    await env.AMEDAS.put("latest.json", obj.body, {
      httpMetadata: {
        contentType: "application/json; charset=utf-8",
        cacheControl: "public, max-age=120",
      },
    });
    await env.AMEDAS.put("latest_time.txt", raw, {
      httpMetadata: {
        contentType: "text/plain; charset=utf-8",
        cacheControl: "public, max-age=120",
      },
    });
  }
  return { newest, fetched };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(collect(env).then(
      r => console.log(`amedas: ${r.newest} まで / 新規 ${r.fetched} スロット`),
      e => console.error(`amedas: 収集に失敗 ${e}`),
    ));
  },

  // 手動確認用。配信には使わない（訪問者に開くと無料枠を食い潰す）
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/run") {
      return new Response("not found", { status: 404 });
    }
    if (!env.RUN_TOKEN || url.searchParams.get("token") !== env.RUN_TOKEN) {
      return new Response("forbidden", { status: 403 });
    }
    try {
      const r = await collect(env);
      return Response.json(r);
    } catch (e) {
      return new Response(String(e), { status: 500 });
    }
  },
};
