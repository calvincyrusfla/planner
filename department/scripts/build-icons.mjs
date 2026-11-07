#!/usr/bin/env node
// Dev-time script: renders department/assets/icon.svg into PNGs, a Windows
// .ico and a macOS .icns used by the desktop shortcut.
//
//   node department/scripts/build-icons.mjs [--src <svg>] [--out <dir>]
//
// Needs Playwright (with Chromium) available at dev time only. It is not a
// runtime dependency of the dashboard; the generated files are committed.

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(here, '..', 'assets');

const PNG_SIZES = [16, 32, 48, 64, 128, 256, 512, 1024];
const ICO_SIZES = [16, 32, 48, 64, 128, 256];
const ICNS_ENTRIES = [
  ['icp4', 16],
  ['icp5', 32],
  ['icp6', 64],
  ['ic07', 128],
  ['ic08', 256],
  ['ic09', 512],
  ['ic10', 1024],
];
const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function parseArgs(argv) {
  const opts = { src: path.join(ASSETS, 'icon.svg'), out: ASSETS };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      const v = argv[++i];
      if (!v) fail(`Missing value for ${a}`);
      return path.resolve(v);
    };
    if (a === '--src') opts.src = next();
    else if (a === '--out') opts.out = next();
    else if (a === '-h' || a === '--help') {
      console.log('Usage: node department/scripts/build-icons.mjs [--src <svg>] [--out <dir>]');
      process.exit(0);
    } else fail(`Unknown argument: ${a}`);
  }
  return opts;
}

function fail(msg) {
  console.error(`build-icons: ${msg}`);
  process.exit(1);
}

async function loadPlaywright() {
  try {
    return createRequire(import.meta.url)('playwright');
  } catch {
    try {
      const mod = await import('playwright');
      return mod.default ?? mod;
    } catch {
      return null;
    }
  }
}

async function renderPngs(svg, sizes) {
  const pw = await loadPlaywright();
  if (!pw || !pw.chromium) {
    fail(
      'Playwright is required to render the icons but could not be loaded.\n' +
        '  It is a dev-time tool only. Install it temporarily, e.g.:\n' +
        '    npm i --no-save playwright && npx playwright install chromium\n' +
        '  then re-run this script.'
    );
  }
  let browser;
  try {
    browser = await pw.chromium.launch();
  } catch (err) {
    fail(`Could not launch Chromium via Playwright: ${err.message}\n  Try: npx playwright install chromium`);
  }
  const dataUrl = 'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64');
  const out = new Map();
  try {
    const context = await browser.newContext({ deviceScaleFactor: 1 });
    const page = await context.newPage();
    for (const size of sizes) {
      await page.setViewportSize({ width: size, height: size });
      await page.setContent(
        `<!doctype html><html><head><style>
          html,body{margin:0;padding:0;background:transparent;overflow:hidden}
          img{display:block;width:${size}px;height:${size}px}
        </style></head><body><img id="i" src="${dataUrl}"></body></html>`
      );
      const ok = await page.evaluate(async () => {
        const img = document.getElementById('i');
        if (!img.complete) await new Promise((r) => { img.onload = r; img.onerror = r; });
        return img.naturalWidth > 0;
      });
      if (!ok) fail('The SVG failed to load in Chromium (is it valid SVG with an xmlns attribute?)');
      const buf = await page.screenshot({
        type: 'png',
        omitBackground: true,
        clip: { x: 0, y: 0, width: size, height: size },
      });
      const { width, height } = pngSize(buf);
      if (width !== size || height !== size) fail(`Rendered ${width}x${height}, expected ${size}x${size}`);
      out.set(size, buf);
    }
  } finally {
    await browser.close();
  }
  return out;
}

function pngSize(buf) {
  if (!buf.subarray(0, 8).equals(PNG_SIG)) throw new Error('not a PNG');
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

function buildIco(pngs) {
  const count = pngs.length;
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(count, 4);
  const entries = [];
  let offset = 6 + 16 * count;
  for (const { size, data } of pngs) {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0); // width (0 = 256)
    e.writeUInt8(size >= 256 ? 0 : size, 1); // height
    e.writeUInt8(0, 2); // color count
    e.writeUInt8(0, 3); // reserved
    e.writeUInt16LE(1, 4); // planes
    e.writeUInt16LE(32, 6); // bit count
    e.writeUInt32LE(data.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += data.length;
    entries.push(e);
  }
  return Buffer.concat([header, ...entries, ...pngs.map((p) => p.data)]);
}

function buildIcns(entries) {
  const chunks = entries.map(({ type, data }) => {
    const h = Buffer.alloc(8);
    h.write(type, 0, 4, 'ascii');
    h.writeUInt32BE(8 + data.length, 4);
    return Buffer.concat([h, data]);
  });
  const body = Buffer.concat(chunks);
  const h = Buffer.alloc(8);
  h.write('icns', 0, 4, 'ascii');
  h.writeUInt32BE(8 + body.length, 4);
  return Buffer.concat([h, body]);
}

async function main() {
  const { src, out } = parseArgs(process.argv.slice(2));
  let svg;
  try {
    svg = await readFile(src, 'utf8');
  } catch {
    fail(`Source SVG not found: ${src}`);
  }
  await mkdir(out, { recursive: true });

  const pngs = await renderPngs(svg, PNG_SIZES);
  for (const [size, data] of pngs) {
    const file = path.join(out, `icon-${size}.png`);
    await writeFile(file, data);
    console.log(`wrote ${path.relative(process.cwd(), file)} (${data.length} bytes)`);
  }

  const ico = buildIco(ICO_SIZES.map((size) => ({ size, data: pngs.get(size) })));
  const icoFile = path.join(out, 'icon.ico');
  await writeFile(icoFile, ico);
  console.log(`wrote ${path.relative(process.cwd(), icoFile)} (${ico.length} bytes, ${ICO_SIZES.join('/')})`);

  const icns = buildIcns(ICNS_ENTRIES.map(([type, size]) => ({ type, data: pngs.get(size) })));
  const icnsFile = path.join(out, 'icon.icns');
  await writeFile(icnsFile, icns);
  console.log(`wrote ${path.relative(process.cwd(), icnsFile)} (${icns.length} bytes, ${ICNS_ENTRIES.map((e) => e[0]).join('/')})`);
}

main().catch((err) => fail(err.stack || String(err)));
