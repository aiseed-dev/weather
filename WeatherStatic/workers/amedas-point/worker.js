/**
 * アメダス地点別データの取得。**1 エリア分を 1 ファイルにまとめて** R2 へ置く。
 *
 * tgsvr から呼ばれる。cron は持たない。いつ・何を取るかを決めるのは tgsvr 側に
 * 寄せる（日付の切り替わりや取りこぼしの追跡が 1 箇所で済む）。Worker は
 * 「重い取得を肩代わりする手足」に徹する。
 *
 * なぜ 1 本か（以前は親子 2 本だった）
 * ------------------------------------
 * 親子に分けたのは「1 実行あたりサブリクエスト 50」に 1,286 地点が収まらな
 * かったため。地点ごとに R2 へ置いていた頃の制約で、**エリア単位にまとめた
 * 時点で 1 エリア＝最大 49（fetch 47 ＋ get 1 ＋ put 1）**になり、
 * 分ける理由が消えた。
 *
 * 加えて cf-publish 0.3.1 に Service Binding の対応が無く、親が子を呼ぶ形は
 * この道具ではデプロイできない。1 本なら tgsvr がエリアごとに呼ぶだけで済み、
 * 結果もエリア単位で返るので、失敗したエリアだけ呼び直せる。
 *
 * 数の勘定（無料枠）
 * ------------------
 *   サブリクエスト 50/実行 … R2 の put も数に入る
 *     ("A subrequest is any request a Worker makes using the Fetch API or to
 *      Cloudflare services like R2, KV, or D1.")
 *     1 エリア最大 47 地点 → fetch 47 ＋ get 1 ＋ put 1 ＝ 49
 *     get は毎回行う（今回書かない地点を引き継ぐため）
 *   実行 10 万/日 … 64 エリア × 144 回 ＝ 9,216/日
 *   R2 Class A 100 万/月 … 64 × 144 × 30 ＝ 27.6 万/月
 *   CPU 10ms … 通常は復号しない。失敗地点があるときだけ既存を復号する（下記）
 *
 * 本文は原則として復号しない
 * --------------------------
 * 気象庁のペイロードをバイト列のまま `{"11001":<生>,"16001":<生>}` と連結する
 * だけにする。文字列に直すと UTF-8 の復号と再符号化で 2 往復ぶん余計に食う。
 * 例外は既存の引き継ぎで、毎回 GET して復号し、今回書かない地点を残す。
 * 実測（407KB・Node 24）で parse 3.9ms / stringify 1.6ms。取得ぶんまで
 * 復号しなければ 10ms に収まる。
 *
 * R2 レイアウト:
 *   point/{YYYYMMDD}/{HH}/{area_code}.json
 *
 * **日付を最上位に置く。** 半月分を NetCDF へ封入したあと生 JSON を消す運用で、
 * 日付が上なら 1 階層の削除で済む。
 *
 * **3 時間ブロック（HH = 00/03/…/21）もキーに残す。** 日単位のキーに書くと、
 * 15 時台の取得が 12 時台までの内容を消してしまう。ブロックごとに分けて
 * おけば、上書きだけで正しく貯まる。
 *
 * 大きさ（実測 2026-08-29 / 東京 44132）: 1 スロット約 528 バイト × 18 スロット
 * ＝ 1 地点 1 ブロック約 9.5KB。エリア 1 本は平均 190KB・最大 450KB。
 *
 * デプロイ（ユーザー実行。合言葉は環境変数から読まれ、表示されない）:
 *   TOKEN=... cf-publish worker deploy . --secret TOKEN --workers-dev
 */

const JMA = "https://www.jma.go.jp/bosai/amedas/data/point";
const UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)";
const CONCURRENCY = 6;      // Workers の同時接続上限に合わせる
const MAX_STATIONS = 48;    // fetch N + get 1 + put 1 ≤ 50

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

/**
 * 取れた地点を 1 つの JSON オブジェクトに連結する（復号しない）。
 * keep があれば、今回取れなかった地点の前回分をそこに足す。
 */
function combine(parts, keep) {
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
  for (const code in keep || {}) {
    push(enc.encode(`${first ? "" : ","}"${code}":${JSON.stringify(keep[code])}`));
    first = false;
  }
  push(enc.encode("}"));

  const out = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}


/**
 * 既に置いてある分のうち、今回書かない地点を拾う。**毎回 GET する。**
 *
 * この器は 3 時間ぶんを溜めていくもので、そのとき書いた地点だけで作り直すと
 * 載らなかった地点が消える。消える原因は取得の失敗だけではない:
 *
 *   - 一時的な失敗や 404
 *   - area_map.json の更新で地点が別エリアへ移った／減った
 *   - 一部の地点だけを指定して呼び直した
 *
 * どれも「今回の依頼に含まれない地点」であり、失敗の有無では判別できない。
 * 常に既存を読んで、今回書かないものはそのまま引き継ぐ。書き込みは加算的に
 * なり、何度呼んでも壊れない。
 *
 * 費用（実測 407KB のエリア束・Node 24）: JSON.parse 3.9ms / stringify 1.6ms。
 * 取得した本文は復号しない（バイト列のまま連結する）ので、ここに乗るのは
 * 既存の復号ぶんだけ。引き継ぐ地点は通常ごく少数なので stringify も小さく、
 * CPU 10ms に収まる。
 */
async function previous(env, key, written) {
  const obj = await env.AMEDAS.get(key);
  if (!obj) return null;
  try {
    const old = JSON.parse(await obj.text());
    const keep = {};
    for (const c in old) if (!written.has(c)) keep[c] = old[c];
    return Object.keys(keep).length ? keep : null;
  } catch (_) {
    return null;      // 壊れていたら諦める（今回ぶんだけ置く）
  }
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
  if (tally.stored === 0) return { area, ...tally, written: 0, kept: 0 };

  const key = `point/${day}/${hour}/${area}.json`;
  const written = new Set(parts.filter((p) => p.state === "stored").map((p) => p.code));
  const keep = await previous(env, key, written);

  await env.AMEDAS.put(key, combine(parts, keep), {
    httpMetadata: {
      contentType: "application/json; charset=utf-8",
      // 進行中のブロックは上書きされ続けるので短命にする
      cacheControl: "public, max-age=600",
    },
  });
  return { area, ...tally, written: 1, kept: keep ? Object.keys(keep).length : 0 };
}

export default {
  /**
   * tgsvr からの呼び出し口。エリアと地点は**呼ぶ側が渡す** — Worker が
   * 地点表を読みに行かないので、地点の増減も tgsvr の一存で反映できる
   * （Worker の再デプロイが要らない）。
   *
   *   POST /fetch
   *   {"day":"20260829","hour":"15","area":"岩手","stations":["33006", …]}
   */
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/fetch") {
      return new Response("not found", { status: 404 });
    }
    if (!env.TOKEN || request.headers.get("Authorization") !== `Bearer ${env.TOKEN}`) {
      return new Response("forbidden", { status: 403 });
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
    // 黙って欠けるより、受け取れない依頼は断る
    if (stations.length > MAX_STATIONS) {
      return new Response(`地点が多すぎます（${stations.length}）。`
                          + `fetch N + get 1 + put 1 で 50 まで、`
                          + `つまり ${MAX_STATIONS} 地点まで`,
                          { status: 400 });
    }
    try {
      return Response.json({ ok: 1, ...await handle(env, area, stations, day, hour) });
    } catch (e) {
      return new Response(String(e), { status: 500 });
    }
  },
};
