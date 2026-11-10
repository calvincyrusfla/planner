import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(here, '..', 'dept.mjs');
const ROOT = path.join(here, '..', '..');

function thisRepoSlug() {
  try {
    const url = execFileSync('git', ['remote', 'get-url', 'origin'], { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    const parts = url.trim().replace(/\/+$/, '').replace(/\.git$/i, '').split(/[/:]/).filter(Boolean);
    return parts.length >= 2 ? parts.slice(-2).join('/') : null;
  } catch {
    return null;
  }
}

const slug = thisRepoSlug();
const needsOrigin = { skip: slug ? false : 'this folder has no origin remote' };

function scratch(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-check-'));
  const env = { ...process.env, DEPT_HUB: 'off', DEPT_DATA: path.join(dir, 'state.json'), DEPT_URL: 'http://127.0.0.1:9', HTTPS_PROXY: '', https_proxy: '' };
  const cli = (...args) => execFileSync(process.execPath, [CLI, ...args], { env, encoding: 'utf8' });
  const batchFile = (repo) => {
    const file = path.join(dir, 'batch.json');
    const subtasks = [{ key: '1', title: 'Probe', description: 'A probe.', files: ['department/lib/order-check.mjs'] }];
    fs.writeFileSync(file, JSON.stringify({ title: 'Check probe', repo, subtasks }));
    return file;
  };
  try {
    body({ env, cli, batchFile });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('foundry check on a batch for this repo prints findings or No findings', needsOrigin, () => {
  scratch(({ cli, batchFile }) => {
    const lines = cli('foundry', 'check', '--json', batchFile(slug)).trim().split(/\r?\n/);
    for (const line of lines) assert.match(line, /^(#\S+ (warn|note) [a-z-]+: .+|No findings)$/);
  });
});

test('foundry check matches the repo case-insensitively', needsOrigin, () => {
  scratch(({ cli, batchFile }) => {
    assert.doesNotMatch(cli('foundry', 'check', '--json', batchFile(slug.toUpperCase())), /check skipped/);
  });
});

test('foundry check on a batch for another repo is skipped', () => {
  scratch(({ cli, batchFile }) => {
    assert.match(cli('foundry', 'check', '--json', batchFile('someone/else')), /^check skipped: this folder is .+, not someone\/else$/m);
  });
});

test('foundry check without a --json value shows the usage', () => {
  scratch(({ env }) => {
    const missing = spawnSync(process.execPath, [CLI, 'foundry', 'check', '--json'], { env, encoding: 'utf8' });
    assert.notEqual(missing.status, 0);
    assert.match(missing.stderr, /Usage: foundry dispatch/);
    assert.match(missing.stderr, /foundry check --json file\.json/);
  });
});

test('foundry dispatch prints the check and still dispatches', needsOrigin, () => {
  scratch(({ cli, batchFile }) => {
    const out = cli('foundry', 'dispatch', '--json', batchFile(slug));
    assert.match(out, /^check: /m);
    assert.match(out, /S-001 dispatched/);
  });
});

test('foundry dispatch for another repo says the check was skipped and dispatches', () => {
  scratch(({ cli, batchFile }) => {
    const out = cli('foundry', 'dispatch', '--json', batchFile('someone/else'));
    assert.match(out, /^check: skipped \(this folder is .+, not someone\/else\)$/m);
    assert.match(out, /S-001 dispatched/);
  });
});
