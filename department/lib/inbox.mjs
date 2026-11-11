// Pure helpers for the dashboard's Inbox view (department/public/index.html carries a
// copy of these, since the page is a classic script). Everything here only reads a board
// state object; nothing renders HTML.

export const INBOX_DAYS = 7;
const OPEN = ['queued', 'claimed', 'running'];
const ms = (iso) => Date.parse(iso || '') || 0;
const arr = (v) => (Array.isArray(v) ? v : []);

// Items that need the user, newest first:
//  help   - a subtask in needs_help of an open batch (answerable)
//  failed - a failed/quarantined subtask updated in the last 7 days
//  batch  - an open batch, with its PRs
export function inboxItems(state, now = Date.now()) {
  const items = [];
  const since = now - INBOX_DAYS * 86400000;
  for (const b of arr(state && state.foundryBatches)) {
    if (!b || typeof b.id !== 'string') continue;
    const open = OPEN.includes(b.status);
    const results = b.results && typeof b.results === 'object' ? b.results : {};
    const prs = [];
    for (const st of arr(b.subtasks)) {
      const r = results[st.key] || {};
      const at = r.updatedAt || b.updatedAt || b.createdAt || '';
      const title = String(st.title || st.key);
      if (r.pr && r.pr.url) prs.push({ key: st.key, title, url: String(r.pr.url), state: r.pr.state || '' });
      if (r.state === 'needs_help' && open && r.help) {
        items.push({
          kind: 'help', id: `${b.id}/${st.key}`, batchId: b.id, key: st.key, title, at,
          question: String(r.help.question || ''), context: String(r.help.context || ''), options: arr(r.help.options).map(String),
        });
      } else if ((r.state === 'failed' || r.state === 'quarantined') && ms(at) >= since) {
        items.push({
          kind: 'failed', id: `${b.id}/${st.key}`, batchId: b.id, key: st.key, title, at,
          state: r.state, error: String(r.error || (r.pr && r.pr.error) || ''),
        });
      }
    }
    if (open) {
      items.push({
        kind: 'batch', id: b.id, batchId: b.id, title: String(b.title || b.id), at: b.updatedAt || b.createdAt || '',
        status: b.status, prs, prCount: prs.length,
      });
    }
  }
  return items.sort((a, b) => ms(b.at) - ms(a.at) || String(b.id).localeCompare(String(a.id)));
}

export const inboxCount = (state, now) => inboxItems(state, now).length;

// The operation the page posts to /api/op; same shape `dept.mjs foundry answer` sends.
export function answerOperation(item, answer, by = 'user') {
  const text = String(answer ?? '').trim();
  if (!item || !item.batchId || !item.key || !text) return null;
  return { type: 'answerHelp', args: { id: item.batchId, key: item.key, answer: text, by } };
}
