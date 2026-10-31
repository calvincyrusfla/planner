// Shared storage for the Juggle Planner on Netlify.
// GET  /api/planner          -> { tabs, tasks, rev }       (anyone with the link)
// POST /api/planner          -> apply edits                (needs the EDIT_KEY passcode)
// Data is one JSON document in Netlify Blobs. It is only ever changed by the
// operations below; nothing here deletes the whole document.
import { getStore } from "@netlify/blobs";
import { timingSafeEqual, createHash } from "node:crypto";
import seed from "./seed.json" with { type: "json" };

const KEY = "planner";
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

const store = () => getStore({ name: "juggle", consistency: "strong" });

async function read(s) {
  const r = await s.getWithMetadata(KEY, { type: "json" });
  if (r && r.data) return { data: r.data, etag: r.etag };
  return { data: { tabs: structuredClone(seed.tabs), tasks: structuredClone(seed.tasks), rev: 0 }, etag: null };
}

function keyOk(given) {
  const want = process.env.EDIT_KEY || "";
  if (!want || !given) return false;
  const h = (x) => createHash("sha256").update(String(x)).digest();
  return timingSafeEqual(h(given), h(want));
}

const sortTabs = (d) => d.tabs.sort((a, b) => (a.order || 0) - (b.order || 0));
const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, "");
const clean = (o) => (o && typeof o === "object" && !Array.isArray(o) ? o : {});

// Merge another copy of the planner in without removing anything already here.
// Tabs match by name; tasks are skipped if the same title + date + tab exists.
export function merge(d, incoming) {
  let addedTabs = 0, addedTasks = 0;
  const tabMap = {};
  for (const t of incoming.tabs || []) {
    if (!t || !t.name) continue;
    let hit = d.tabs.find((x) => x.id === t.id) || d.tabs.find((x) => norm(x.name) === norm(t.name));
    if (!hit) {
      hit = { id: t.id || Math.random().toString(36).slice(2, 10), name: t.name, color: t.color || "#6B7A8C",
              order: d.tabs.reduce((m, x) => Math.max(m, x.order || 0), 0) + 1 };
      if (d.tabs.some((x) => x.id === hit.id)) hit.id += Math.random().toString(36).slice(2, 6);
      d.tabs.push(hit); addedTabs++;
    }
    tabMap[t.id] = hit.id;
  }
  for (const t of incoming.tasks || []) {
    if (!t || !t.title) continue;
    const tab = tabMap[t.tab] || (d.tabs.some((x) => x.id === t.tab) ? t.tab : null);
    if (!tab) continue;
    const dup = d.tasks.some((x) => x.id === t.id ||
      (x.tab === tab && norm(x.title) === norm(t.title) && (x.due || "") === (t.due || "")));
    if (dup) continue;
    d.tasks.push({ ...t, tab, id: d.tasks.some((x) => x.id === t.id) || !t.id ? Math.random().toString(36).slice(2, 12) : t.id });
    addedTasks++;
  }
  sortTabs(d);
  return { addedTabs, addedTasks };
}

export function apply(d, op) {
  const col = op.col === "tabs" ? "tabs" : op.col === "tasks" ? "tasks" : null;
  if (op.op === "merge") return merge(d, clean(op.data));
  if (!col || typeof op.id !== "string" || !op.id) throw new Error("bad op");
  const list = d[col];
  if (op.op === "add") { if (!list.some((x) => x.id === op.id)) list.push({ ...clean(op.data), id: op.id }); }
  else if (op.op === "upd") { const x = list.find((x) => x.id === op.id); if (x) Object.assign(x, clean(op.data), { id: op.id }); }
  else if (op.op === "del") { d[col] = list.filter((x) => x.id !== op.id); }
  else throw new Error("bad op");
  if (col === "tabs") sortTabs(d);
  return {};
}

export default async (req) => {
  const s = store();
  if (req.method === "GET") {
    const { data } = await read(s);
    return json({ ...data, canEdit: keyOk(req.headers.get("x-edit-key")), editingEnabled: !!process.env.EDIT_KEY });
  }
  if (req.method !== "POST") return json({ error: "method" }, 405);
  if (!process.env.EDIT_KEY) return json({ error: "editing_disabled" }, 403);
  if (!keyOk(req.headers.get("x-edit-key"))) return json({ error: "bad_key" }, 401);
  let body;
  try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400); }
  const ops = Array.isArray(body.ops) ? body.ops : [];
  for (let attempt = 0; attempt < 6; attempt++) {
    const { data, etag } = await read(s);
    let result = {};
    try { for (const op of ops) result = apply(data, op) || result; } catch (e) { return json({ error: "bad_op" }, 400); }
    data.rev = (data.rev || 0) + 1;
    const w = await s.setJSON(KEY, data, etag ? { onlyIfMatch: etag } : { onlyIfNew: true });
    if (w.modified) return json({ ...data, canEdit: true, editingEnabled: true, result });
  }
  return json({ error: "busy" }, 409);
};

export const config = { path: "/api/planner" };
