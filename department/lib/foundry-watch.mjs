// Pure event logic of `foundry watch`: compares two views of the Foundry batches
// and says what needs the Floor Manager's attention. No timers and no I/O, so
// the polling loop stays in dept.mjs and this can be tested on plain arrays.

import { OPEN_BATCH } from './foundry-batches.mjs';

const STALE_MS = 10 * 60 * 1000;
const FAILED_RESULT = new Set(['quarantined', 'failed']);

// Errors and questions can span lines; an event stays on one.
const oneLine = (value) => String(value).replace(/\s+/g, ' ').trim();

function subtaskEvents(b, st, r, p) {
  const base = { batch: b.id, key: st.key, foundryTask: r.foundryTask || null, title: st.title };
  const label = `${b.id} #${st.key} ${r.foundryTask || '—'} ${oneLine(st.title)}`;
  const events = [];

  // Counts even when it was already waiting at the start, so a question is never
  // missed. A closed batch is left out because answerHelp refuses it.
  if (r.state === 'needs_help' && OPEN_BATCH.has(b.status)) {
    const question = r.help?.question || '—';
    const answer = `node department/dept.mjs foundry answer ${b.id} ${st.key} --guidance "..."`;
    events.push({ kind: 'HELP', ...base, question, answer, line: `HELP  ${label}: ${oneLine(question)}\n  answer: ${answer}` });
  }

  const url = r.pr?.url || null;
  if ((r.state === 'complete' && p.state !== 'complete') || (url && url !== p.pr?.url)) {
    events.push({ kind: 'DONE', ...base, url, line: `DONE  ${label} PR ${url || '—'}` });
  }

  const failedNow = FAILED_RESULT.has(r.state) && !FAILED_RESULT.has(p.state);
  const prFailed = r.pr?.state === 'failed' && p.pr?.state !== 'failed';
  if (failedNow || prFailed) {
    const error = r.error || r.pr?.error || (failedNow ? r.state : 'PR failed');
    events.push({ kind: 'FAIL', ...base, error, line: `FAIL  ${label}: ${oneLine(error)}` });
  }
  return events;
}

const BAD_UPDATE = new Set(['refused', 'failed']);

// An UPDATE event for every update request refused or failed in `after` that was
// not already refused or failed in `before`.
export function updateEvents(beforeUpdates, afterUpdates) {
  const was = new Map((Array.isArray(beforeUpdates) ? beforeUpdates : []).map((u) => [u.id, u]));
  const events = [];
  for (const u of Array.isArray(afterUpdates) ? afterUpdates : []) {
    if (!u || !BAD_UPDATE.has(u.status)) continue;
    if (BAD_UPDATE.has(was.get(u.id)?.status)) continue;
    const message = oneLine(u.result?.message || '').slice(0, 200);
    events.push({ kind: 'UPDATE', update: u.id, status: u.status, line: `UPDATE ${u.id} ${u.status}: ${message}` });
  }
  return events;
}

function heartbeatAge(b, nowMs) {
  const at = Date.parse(b.heartbeatAt || b.claimedBy?.at || '');
  return Number.isFinite(at) ? nowMs - at : NaN;
}

// `before` is the snapshot taken at the start of the watch and must hold every
// batch that existed then, closed ones too: a batch missing from it counts as
// dispatched during the watch. `after` is the latest poll, already narrowed to
// the watched batches. Returns [{kind: HELP|DONE|FAIL|NOTE|BATCH|STALE, batch, line,
// ...details}] (UPDATE events come from updateEvents, not from this function); `line` is the text to print. Queue progress (queued, sizing,
// building, reviewing, answered) produces nothing.
export function watchEvents(before, after, nowMs) {
  const known = new Map((before || []).map((b) => [b.id, b]));
  const events = [];
  for (const b of after || []) {
    const prev = known.get(b.id);
    for (const st of b.subtasks || []) {
      events.push(...subtaskEvents(b, st, b.results?.[st.key] || {}, prev?.results?.[st.key] || {}));
    }
    if (typeof b.note === 'string' && b.note && b.note !== (typeof prev?.note === 'string' ? prev.note : '')) {
      events.push({ kind: 'NOTE', batch: b.id, note: b.note, line: `NOTE  ${b.id} ${b.note}` });
    }
    if ((!prev || OPEN_BATCH.has(prev.status)) && !OPEN_BATCH.has(b.status)) {
      events.push({ kind: 'BATCH', batch: b.id, status: b.status, line: `BATCH ${b.id} ${b.status}` });
    }
    if (b.status === 'claimed' || b.status === 'running') {
      const age = heartbeatAge(b, nowMs);
      if (age > STALE_MS) {
        const minutes = Math.floor(age / 60000);
        events.push({ kind: 'STALE', batch: b.id, minutes, line: `STALE ${b.id} no Foundry heartbeat for ${minutes} min` });
      }
    }
  }
  return events;
}
