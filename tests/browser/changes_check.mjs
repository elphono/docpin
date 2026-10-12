// Drives "what changed since the last send" in a real headless Chromium.
//
//   node changes_check.mjs <playwright-dir> <base-url> <doc-id> <out-dir> <doc-path>
//
// The Python side (tests/test_browser.py) sent version A of the document,
// wrote version B, and checks afterwards what reached the server. First the
// pure comparison (diff.js) on crafted pairs, then the page as the reader
// sees it. Each step prints one line; any failure exits non-zero.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [pwDir, base, docId, outDir, docPath] = process.argv.slice(2);
const require = createRequire(join(pwDir, 'package.json'));
const { chromium } = require('playwright');

const fail = (msg) => { console.error('FAIL ' + msg); process.exit(1); };
const step = (msg) => console.log('OK   ' + msg);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(`${base}/docs/${docId}`);
  await page.waitForSelector('annotate-root', { state: 'attached' });

  const pure = await page.evaluate(() => {
    const D = window.__annotateDiff;
    const wrap = (b) => `<!doctype html><html><body>${b}</body></html>`;
    const kinds = (r) => r.changes.map((c) => c.kind).join(',');
    const out = {};
    out.same = kinds(D.compare(wrap('<p>a b</p><p>c d</p>'), wrap('<p>a b</p><p>c d</p>'), document));
    out.nested = kinds(D.compare(wrap('<ul><li>Intro words here<ul><li>child</li></ul></li></ul>'),
      wrap('<ul><li>Intro words there<ul><li>child</li></ul></li></ul>'), document));
    out.figure = kinds(D.compare(wrap('<svg><rect fill="red"/><text>Label</text></svg>'),
      wrap('<svg><rect fill="blue"/><text>Label</text></svg>'), document));
    out.moved = kinds(D.compare(wrap('<p>one alpha</p><p>two beta</p><p>three gamma</p>'),
      wrap('<p>two beta</p><p>three gamma</p><p>one alpha</p>'), document));
    const many = (p) => Array.from({ length: 2100 }, (_, i) => `<p>${p} ${i}</p>`).join('');
    out.tooMany = D.compare(wrap(many('old')), wrap(many('new')), document).tooMany;
    const ops = D.myers(['a', 'b', 'c', 'a', 'b', 'b', 'a'], ['c', 'b', 'a', 'b', 'a', 'c'], 99);
    out.myersEdits = ops.filter((o) => o[0] !== '=').length;
    return out;
  });
  if (pure.same !== '') fail('witness: identical versions gave ' + pure.same);
  if (pure.nested !== 'changed') fail('parent li own text: ' + pure.nested);
  if (pure.figure !== 'changed') fail('svg colour: ' + pure.figure);
  if (pure.moved !== 'new,removed' && pure.moved !== 'removed,new') fail('moved block: ' + pure.moved);
  if (pure.tooMany !== true) fail('2100 differences were compared');
  if (pure.myersEdits !== 5) fail('myers is not minimal: ' + pure.myersEdits + ' edits');
  step('pure comparison: witness, nested li, figure, moved, too many, minimal script');

  const shadow = (sel) => page.evaluate((s) =>
    [...document.querySelector('annotate-root').shadowRoot.querySelectorAll(s)].map((n) => n.textContent), sel);
  const barText = () => page.evaluate(() =>
    document.querySelector('annotate-root').shadowRoot.querySelector('.bar').textContent);
  await page.waitForFunction(() => document.querySelector('annotate-root').shadowRoot
    .querySelector('.bar').textContent.includes('Changes since your last send'));
  step('changes panel shown');

  // 1. Exactly one new, one changed, one removed, on the right blocks.
  const marks = await page.evaluate(() => ({
    news: [...document.querySelectorAll('.annotate-new')].map((n) => n.textContent),
    changed: [...document.querySelectorAll('.annotate-changed')].map((n) => n.textContent),
  }));
  if (marks.news.length !== 1 || !marks.news[0].includes('fourth option')) fail('new: ' + JSON.stringify(marks.news));
  if (marks.changed.length !== 1 || !marks.changed[0].includes('two days')) fail('changed: ' + JSON.stringify(marks.changed));
  const pills = await shadow('.chg-removed');
  if (pills.length !== 1) fail('removed markers: ' + pills.length);
  step('1 new, 1 changed, 1 removed');

  // 2. Added words are highlighted, the unchanged ones are not.
  const words = await page.evaluate(() => {
    const h = CSS.highlights && CSS.highlights.get('annotate-added');
    return h ? [...h].map((r) => r.toString()) : null;
  });
  if (!words || !words.includes('two') || !words.includes('days') || words.includes('migration')) fail('words: ' + JSON.stringify(words));
  step('added words highlighted: ' + words.join(' '));

  // 3. Unchanged sections fold, heading kept; the one with an unsent note does not.
  const folds = await page.evaluate(() => {
    const p = [...document.querySelectorAll('body > p')];
    const shown = (n) => getComputedStyle(n).display !== 'none';
    return { count: document.querySelectorAll('annotate-fold').length,
      ctx: shown(p[1]) || shown(p[2]), unsent: shown(p[6]), heading: shown(document.getElementById('s1')),
      dim: p[3].classList.contains('annotate-dim') };
  });
  if (folds.count !== 2 || folds.ctx || !folds.unsent || !folds.heading || !folds.dim) fail('folding: ' + JSON.stringify(folds));
  step('Context and Decision folded, Notes open, unchanged Options block dimmed');
  await page.screenshot({ path: join(outDir, 'changes-folded.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'changes-folded-dark.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'light' });

  // 4. A sent pin in a folded section is hidden; the unsent one is on its block.
  const pinsState = await page.evaluate(() => {
    const pins = [...document.querySelector('annotate-root').shadowRoot.querySelectorAll('.pin')];
    return pins.map((p) => ({ text: p.textContent, hidden: p.style.display === 'none' }));
  });
  const sentPin = pinsState.find((p) => p.text.includes('✓'));
  if (!sentPin || !sentPin.hidden) fail('sent pin in a fold: ' + JSON.stringify(pinsState));
  step('treated pin hidden in its folded section');

  // 5. The navigator walks the three changes and wraps, three rounds.
  const nav = async (label) => {
    await page.evaluate((l) => [...document.querySelector('annotate-root').shadowRoot
      .querySelectorAll('.chg-nav button')].find((b) => b.textContent === l).click(), label);
    return (await shadow('.chg-nav span'))[0];
  };
  const seen = [];
  for (let i = 0; i < 4; i++) seen.push(await nav('▼'));
  if (seen.join('|') !== '1 / 3|2 / 3|3 / 3|1 / 3') fail('navigator down: ' + seen.join('|'));
  if (await nav('▲') !== '3 / 3') fail('navigator up does not wrap');
  step('navigator: ' + seen.join(' → ') + ', ▲ wraps to 3 / 3');

  // 6. Alt+click while folded: the note anchors to the block aimed at.
  const target = page.locator('body > p').nth(6);
  const box = await target.boundingBox();
  await page.keyboard.down('Alt');
  await page.mouse.click(box.x + 30, box.y + box.height / 2);
  await page.keyboard.up('Alt');
  await page.waitForSelector('.bubble textarea');
  await page.locator('.bubble textarea').fill('Third note, while folded.');
  await page.locator('.bubble textarea').press('Enter');
  await page.waitForFunction(async (api) => (await (await fetch(api)).json())
    .annotations.some((a) => a.note === 'Third note, while folded.'), `/api/docs/${docId}/annotations`);
  const foldClick = await page.evaluate(() => {
    const f = document.querySelector('annotate-fold');
    const r = f.getBoundingClientRect();
    return { x: r.left + 20, y: r.top + r.height / 2 };
  });
  await page.keyboard.down('Alt');
  await page.mouse.click(foldClick.x, foldClick.y);
  await page.keyboard.up('Alt');
  if (await page.locator('.bubble textarea').count()) fail('a note can be pinned on the fold bar');
  step('note pinned while folded; none on the fold bar');

  // 7. A link into a folded section unfolds it first.
  await page.locator('a[href="#ctx-two"]').click();
  if (await page.evaluate(() => getComputedStyle(document.getElementById('ctx-two')).display) === 'none') fail('link into a fold');
  const pinBack = await page.evaluate(() => {
    const p = [...document.querySelector('annotate-root').shadowRoot.querySelectorAll('.pin')]
      .find((n) => n.textContent.includes('✓'));
    const b = document.querySelectorAll('body > p')[1].getBoundingClientRect();
    const r = p.getBoundingClientRect();
    return p.style.display !== 'none' && r.top >= b.top - 14 && r.top <= b.bottom + 14;
  });
  if (!pinBack) fail('the treated pin did not come back on its block');
  step('link unfolded Context; the treated pin is back on its block');

  // 8. The removed marker shows the old text; Show everything unfolds and undims.
  await page.evaluate(() => document.querySelector('annotate-root').shadowRoot.querySelector('.chg-removed').click());
  const old = await shadow('.chg-old');
  await page.screenshot({ path: join(outDir, 'changes-removed.png') });
  if (!old.some((t) => t.includes('Option C needs one more server'))) fail('old text: ' + JSON.stringify(old));
  await page.evaluate(() => [...document.querySelector('annotate-root').shadowRoot
    .querySelectorAll('.chg-actions button')].find((b) => b.textContent === 'Show everything').click());
  const after = await page.evaluate(() => ({ folded: document.querySelectorAll('.annotate-folded').length,
    dim: document.querySelectorAll('.annotate-dim').length, fold: document.querySelectorAll('annotate-fold').length }));
  if (after.folded || after.dim || after.fold) fail('show everything: ' + JSON.stringify(after));
  await page.screenshot({ path: join(outDir, 'changes-open.png'), fullPage: true });
  step('removed text shown struck; Show everything unfolded and undimmed');

  // 9. A change the page cannot find (a script rewrote it): counted, and nothing folds.
  const source = readFileSync(docPath, 'utf8');
  writeFileSync(docPath, source.replace('Clock reads ten past nine', 'Clock reads ten past ten'));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('annotate-root').shadowRoot
    .querySelector('.bar').textContent.includes('not located'));
  if (await page.evaluate(() => document.querySelectorAll('annotate-fold').length)) fail('folded with a lost change');
  step('lost change counted, nothing folded');

  // 10. Mark as read, reload: the witness, nothing at all.
  // Wait on the navigation itself: every condition of the page below was
  // already true on the page BEFORE the reload.
  await Promise.all([
    page.waitForNavigation(),
    page.evaluate(() => [...document.querySelector('annotate-root').shadowRoot
      .querySelectorAll('.chg-actions button')].find((b) => b.textContent === 'Mark as read').click()),
  ]);
  await page.waitForSelector('annotate-root', { state: 'attached' });
  await page.waitForFunction(() => document.querySelector('annotate-root') &&
    document.querySelector('annotate-root').shadowRoot.querySelector('.bar').textContent.includes('to send'));
  const witness = await page.evaluate(() => document.querySelectorAll('.annotate-new,.annotate-changed,annotate-fold,.annotate-dim').length);
  if (witness || (await barText()).includes('Changes since')) fail('after Mark as read: ' + witness + ' marks');
  step('Mark as read: no mark, no fold, no panel');
  if (errors.length) fail('page errors: ' + errors.join(' | '));
} finally {
  await browser.close();
}
