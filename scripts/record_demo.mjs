// Record the README/join-page demo: Bean triaging the public demo inbox, green → yellow → red.
//
//   BEAN_DATA_DIR=/tmp/rec .venv/bin/python scripts/seed_demo_tenant.py
//   BEAN_DATA_DIR=/tmp/rec BEAN_CUSTOMER=sablewren BEAN_DEMO_TENANT=1 BEAN_DEMO_READONLY=1 \
//     PORT=8756 .venv/bin/python -m bean.server &
//   node scripts/record_demo.mjs --url http://127.0.0.1:8756
//
// Writes assets/demo.gif and web/demo.mp4.
//
// WHY THIS IS A SCRIPT AND NOT A BROWSING SESSION. Three attempts at this failed by driving the
// live UI to discover the DOM: the app long-polls, so `networkidle` never fires and an unbounded
// `waitForSelector` hangs forever rather than failing. Every selector below was instead READ out of
// the source, which cannot hang:
//
//   button.inbox-row   web/bean-inbox.jsx  InboxRow()      — one per email; no id attribute, so
//                                                            rows are addressed by subject text
//   button.back-btn    web/bean-root.jsx   '← inbox'
//   .why-block         web/bean-draft.jsx  the confidence rationale — the frame that matters
//
// Every call below takes an explicit timeout. There is no networkidle and no unbounded wait. If it
// breaks, fix it here and re-run — do not fall back to clicking by hand.
//
// PLAYWRIGHT LIVES IN tests/js, NEVER AT THE REPO ROOT. A root package.json makes Railway's nixpacks
// builder detect a Node project and ship an image with no Python in it; that took production down
// once. It is installed --no-save because the recording is a maintainer chore and a browser download
// has no business in the documented `npm install --prefix tests/js` test setup.

import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, renameSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const FFMPEG = process.env.FFMPEG || '/opt/homebrew/bin/ffmpeg';

const require = createRequire(join(REPO, 'tests/js/'));
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  console.error(
    'playwright not found in tests/js.\n' +
    '  npm install --no-save --prefix tests/js playwright@1.62.1\n' +
    '(1.62.1 matches the chromium already cached in ~/Library/Caches/ms-playwright.)'
  );
  process.exit(1);
}

const arg = (flag, fallback) => {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const URL_ = arg('--url', 'http://127.0.0.1:8756');
const OUT = join(REPO, 'assets');
const VID = join(REPO, '.record-tmp');
// The capture is kept, not deleted. Hitting a GIF size budget takes several encode passes, and
// re-driving the browser for each one is both slow and needlessly non-deterministic — the same
// source video should produce every candidate. `--encode-only` re-runs just the ffmpeg half.
const SRC = join(VID, 'source.webm');
const ENCODE_ONLY = process.argv.includes('--encode-only');

// The click path, and why these three. They are the three confidence lanes in one pass; picking
// three of the same colour would show the UI but not the argument.
//
// sw-mattress is the important one. The notebook mentions mattresses in the restocking policy but
// carries no mattress facts, so Bean says drafting a recommendation would mean inventing them —
// a refusal, with the reason. It gets the longest dwell because it is the whole thesis on screen.
const BEATS = [
  { subject: 'Which fabric for a house with two dogs?', lane: 'green',  dwell: 3000 },
  { subject: "Bed frame slats don't line up",           lane: 'yellow', dwell: 5500 },
  { subject: 'Which mattress should I get?',            lane: 'red',    dwell: 6500 },
];

const T = 20000;              // every wait is bounded by this
const settle = (page, ms) => page.waitForTimeout(ms);

async function capture() {
  rmSync(VID, { recursive: true, force: true });
  mkdirSync(VID, { recursive: true });

  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      recordVideo: { dir: VID, size: { width: 1280, height: 800 } },
      deviceScaleFactor: 1,
      reducedMotion: 'no-preference',
    });
    const page = await ctx.newPage();
    page.setDefaultTimeout(T);

    // domcontentloaded, never networkidle: the app polls /api/inbox forever, so the network is
    // never idle and this would time out on a perfectly healthy page.
    await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: T });

    // The real "is it ready" signal. On the demo tenant the onboarding overlay is skipped by the
    // server flag (window.BEAN_DEMO_TENANT, web/bean-root.jsx), so rows are the first thing up.
    await page.locator('button.inbox-row').first().waitFor({ state: 'visible', timeout: T });

    await settle(page, 3200);                                    // "Morning. 11 emails came in…"
    await page.evaluate(() => window.scrollTo({ top: 240, behavior: 'smooth' }));
    await settle(page, 1600);                                    // the three lanes, colour-coded
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
    await settle(page, 800);

    for (const beat of BEATS) {
      const row = page.locator('button.inbox-row', { hasText: beat.subject }).first();
      await row.waitFor({ state: 'visible', timeout: T });
      await row.click({ timeout: T });

      const back = page.locator('button.back-btn').first();
      await back.waitFor({ state: 'visible', timeout: T });      // detail view is up
      await settle(page, 700);

      // Put the rationale on screen. Green renders a why-block too ("why me's sure"), so this is
      // the same move for all three lanes; fall back to a fixed scroll if the layout ever changes.
      const why = page.locator('.why-block').first();
      if (await why.count()) {
        await why.scrollIntoViewIfNeeded({ timeout: T }).catch(() => {});
      } else {
        await page.evaluate(() => window.scrollTo({ top: 300, behavior: 'smooth' }));
      }
      await settle(page, beat.dwell);

      await back.click({ timeout: T });
      await page.locator('button.inbox-row').first().waitFor({ state: 'visible', timeout: T });
      await settle(page, 900);
    }

    await settle(page, 600);
    await ctx.close();                                           // flushes the webm
  } finally {
    await browser.close();
  }

  const written = readdirSync(VID).find(f => f.endsWith('.webm'));
  if (!written) throw new Error('no video was written');
  renameSync(join(VID, written), SRC);
}

function encode() {
  if (!existsSync(SRC)) throw new Error(`no capture at ${SRC} — run without --encode-only first`);
  mkdirSync(OUT, { recursive: true });
  const webm = SRC;

  // Two-pass palettegen/paletteuse. A naive `ffmpeg -i in.webm out.gif` quantises per frame and
  // produces a muddy, much larger file; this builds one palette across the whole clip.
  const gif = join(OUT, 'demo.gif');
  const pal = join(VID, 'palette.png');
  const W = process.env.GIF_WIDTH || '760';
  const FPS = process.env.GIF_FPS || '10';
  const COLORS = process.env.GIF_COLORS || '128';
  const run = (args) => execFileSync(FFMPEG, ['-v', 'error', '-y', ...args], { stdio: 'inherit' });

  run(['-i', webm, '-vf', `fps=${FPS},scale=${W}:-1:flags=lanczos,palettegen=stats_mode=diff:max_colors=${COLORS}`, pal]);
  run(['-i', webm, '-i', pal, '-lavfi',
       `fps=${FPS},scale=${W}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`,
       gif]);

  // H.264 + faststart so the /join <video> starts playing before the whole file arrives.
  const mp4 = join(REPO, 'web', 'demo.mp4');
  run(['-i', webm, '-vf', 'scale=1280:-2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
       '-crf', '23', '-movflags', '+faststart', '-an', mp4]);

  const size = (p) => (execFileSync('/bin/sh', ['-c', `wc -c < "${p}"`]).toString().trim() / 1e6).toFixed(2);
  console.log(`assets/demo.gif  ${size(gif)} MB  (${W}px, ${FPS}fps, ${COLORS} colours)`);
  console.log(`web/demo.mp4     ${size(mp4)} MB`);
  console.log(`capture kept at ${SRC} — re-tune with:`);
  console.log('  GIF_WIDTH=640 GIF_FPS=8 GIF_COLORS=64 node scripts/record_demo.mjs --encode-only');
}

if (!ENCODE_ONLY) await capture();
encode();
