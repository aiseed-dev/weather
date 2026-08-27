/**
 * アメダス地点別データの取得（親）。cron で起き、子を並列に呼ぶだけ。
 *
 * なぜ親子に分けるか: 無料枠は **1 実行あたりサブリクエスト 50 件**。
 * 1,286 地点を 1 実行では取れない。子をステートレス（地点リストを引数で渡す）に
 * すると、**同じ子スクリプトを何回でも並列に呼べる**ので、スクリプトは
 * 親と子の 2 本で済む（Worker を 68 本デプロイする必要はない）。
 *
 * cron を 2 個使い、親を 2 つ動かして地点を半分ずつ担当する:
 *   親 = 33 サブリクエスト（子の呼び出し）  ≤ 50
 *   子 = 約 20 サブリクエスト（気象庁への取得）≤ 50
 * どちらも上限に余裕がある。cron は 2/5 しか使わない。
 *
 * 地点の並びは府県予報区（prec_no）順。ブロックが地理的にまとまるので、
 * 障害時にどの地域が欠けたか分かりやすい。
 *
 * 環境変数:
 *   HALF   "0" or "1"  … 担当する半分
 *   CHILD  Service Binding … 子 Worker
 *   AMEDAS R2 バケット … 地点索引の読み出しに使う
 */

const KIDS = 33;            // 1 親あたりの子呼び出し数（サブリクエスト 50 の内側）
const PARENTS = 2;

/** 3 時間ブロックのファイル名部（JST）。地点別ファイルは 1 本で 3 時間分を含む */
function currentBlock(nowMs) {
  const jst = new Date(nowMs + 9 * 3600 * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  const h = Math.floor(jst.getUTCHours() / 3) * 3;
  return `${jst.getUTCFullYear()}${p(jst.getUTCMonth() + 1)}${p(jst.getUTCDate())}_${p(h)}`;
}

async function run(env) {
  // 地点一覧は R2 から読む（埋め込むと地点の増減のたびに再デプロイになる）。
  // R2 の読み出しはサブリクエストを消費しない。
  const obj = await env.AMEDAS.get("station/index.json");
  if (!obj) throw new Error("station/index.json が無い（tgsvr 側の生成待ち）");
  const index = await obj.json();
  const all = Object.keys(index.stations || index).sort();

  const half = Number(env.HALF || 0);
  const mine = all.filter((_, i) => i % PARENTS === half);
  const block = currentBlock(Date.now());

  const size = Math.ceil(mine.length / KIDS);
  const calls = [];
  for (let i = 0; i < mine.length; i += size) {
    const chunk = mine.slice(i, i + size);
    calls.push(env.CHILD.fetch("https://child/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stations: chunk, block }),
    }).then(r => r.json(), e => ({ ok: 0, error: String(e) })));
  }
  const res = await Promise.all(calls);
  const got = res.reduce((n, r) => n + (r.stored || 0), 0);
  const failed = res.reduce((n, r) => n + (r.failed || 0), 0);
  return { half, block, stations: mine.length, children: calls.length, got, failed };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env).then(
      r => console.log(`point[${r.half}] ${r.block}: ${r.got}/${r.stations} 保存`
                     + (r.failed ? ` / 失敗 ${r.failed}` : "")),
      e => console.error(`point[${env.HALF}]: ${e}`),
    ));
  },
};
