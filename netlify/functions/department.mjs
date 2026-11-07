// Shared Agent Department board (the "hub") on Netlify.
// GET  /api/department       -> { ok, state, rev }               (needs the EDIT_KEY passcode)
// POST /api/department       -> { type, args } operation         (needs the EDIT_KEY passcode)
//                            -> { ok, result, state, rev }
// The board is private, so both reads and writes need the passcode in the
// x-edit-key header. Data is one JSON document { state, rev } in Netlify Blobs
// (store "department", key "state"), only changed through applyOp() with
// conditional writes so concurrent sessions never overwrite each other.
import { timingSafeEqual, createHash } from "node:crypto";
import * as core from "../../department/lib/core.mjs";
import { applyOp } from "../../department/lib/ops.mjs";

const KEY = "state";
const ATTEMPTS = 6;
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

// Loaded lazily so tests can import makeHandler without node_modules installed.
const defaultStore = async () => {
  const { getStore } = await import("@netlify/blobs");
  return getStore({ name: "department", consistency: "strong" });
};

async function read(s) {
  const r = await s.getWithMetadata(KEY, { type: "json" });
  if (r && r.data && r.data.state) return { state: core.normalize(r.data.state), rev: r.data.rev || 0, etag: r.etag };
  return { state: core.createInitialState(), rev: 0, etag: null };
}

function keyOk(given) {
  const want = process.env.EDIT_KEY || "";
  if (!want || !given) return false;
  const h = (x) => createHash("sha256").update(String(x)).digest();
  return timingSafeEqual(h(given), h(want));
}

export function makeHandler(getStoreFn = defaultStore) {
  return async (req) => {
    if (req.method !== "GET" && req.method !== "POST") return json({ ok: false, error: "method" }, 405);
    if (!process.env.EDIT_KEY) return json({ ok: false, error: "editing_disabled" }, 403);
    if (!keyOk(req.headers.get("x-edit-key"))) return json({ ok: false, error: "bad_key" }, 401);
    const s = await getStoreFn();

    if (req.method === "GET") {
      const { state, rev } = await read(s);
      return json({ ok: true, state, rev });
    }

    let body;
    try { body = await req.json(); } catch { return json({ ok: false, error: "bad_json" }, 400); }
    if (!body || typeof body !== "object" || typeof body.type !== "string") return json({ ok: false, error: "bad_op" }, 400);
    const op = { type: body.type, args: body.args && typeof body.args === "object" ? body.args : {} };

    for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
      const { state, rev, etag } = await read(s);
      let result;
      try {
        result = applyOp(state, op);
      } catch (e) {
        if (e instanceof core.DeptError) return json({ ok: false, error: e.message }, e.status || 400);
        return json({ ok: false, error: "bad_op" }, 400);
      }
      const next = { state, rev: rev + 1 };
      const w = await s.setJSON(KEY, next, etag ? { onlyIfMatch: etag } : { onlyIfNew: true });
      if (w && w.modified) return json({ ok: true, result: result === undefined ? null : result, state, rev: next.rev });
    }
    return json({ ok: false, error: "busy" }, 409);
  };
}

export default makeHandler();

export const config = { path: "/api/department" };
