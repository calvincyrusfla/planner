import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInitialState, normalize } from './core.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
export const DATA_FILE = process.env.DEPT_DATA || path.join(here, '..', 'data', 'state.json');

export function load(file = DATA_FILE) {
  try {
    return normalize(JSON.parse(fs.readFileSync(file, 'utf8')));
  } catch (err) {
    if (err.code !== 'ENOENT') console.warn(`Could not read ${file} (${err.message}); starting fresh`);
    return createInitialState();
  }
}

export function save(state, file = DATA_FILE) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
  fs.renameSync(tmp, file);
}
