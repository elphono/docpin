// Drives "what changed since the last send" in a real headless Chromium.
//
//   node changes_check.mjs <playwright-dir> <base-url> <doc-id> <out-dir> <doc-path> \
//        <nested-doc-id> <mixed-doc-id> <siblings-doc-id> <lists-doc-id>
//
// The Python side (tests/test_browser.py) sent version A of the document,
// wrote version B, and checks afterwards what reached the server. First the
// pure comparison (diff.js) on crafted pairs, then the page as the reader
// sees it, then four documents of blocks inside blocks. Each step prints
// one line; any failure exits non-zero.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [pwDir, base, docId, outDir, docPath, nestedId, mixedId, siblingsId, listsId] = process.argv.slice(2);
if (!nestedId || !mixedId || !siblingsId || !listsId) {
  console.error('FAIL the nested, mixed, siblings and lists document ids are missing');
  process.exit(1);
}
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

  // 1. An addition, a modification, a removal: exactly one mark each, on the
  // right blocks (none of them inside another; nested blocks: steps 11, 12).
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
  // The bar shows "to send" BEFORE the comparison ends: wait on its own state.
  await page.waitForFunction(() => {
    const host = document.querySelector('annotate-root');
    return host && host.dataset.changes && host.dataset.changes !== 'pending';
  });
  if (await page.evaluate(() => document.querySelector('annotate-root').dataset.changes) !== 'none') {
    fail('after Mark as read, the comparison state is not "none"');
  }
  const witness = await page.evaluate(() => document.querySelectorAll('.annotate-new,.annotate-changed,annotate-fold,.annotate-dim').length);
  if (witness || (await barText()).includes('Changes since')) fail('after Mark as read: ' + witness + ' marks');
  step('Mark as read: no mark, no fold, no panel');

  // Blocks inside blocks. The invariant above, for nesting: a block inside a
  // NEW block says nothing more than it, so the topmost new block carries the
  // ONLY mark (class, tag, step, row); under a CHANGED block, a new or changed
  // block keeps its own mark, which is another piece of news.
  // A resize re-renders on the frame AFTER the ResizeObserver: wait past it.
  const frames = () => page.evaluate(() => new Promise((r) =>
    setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(r)), 50)));
  const settled = async (id) => {
    await page.goto(`${base}/docs/${id}`);
    await page.waitForFunction(() => {
      const host = document.querySelector('annotate-root');
      return host && host.dataset.changes && host.dataset.changes !== 'pending';
    });
    const state = await page.evaluate(() => document.querySelector('annotate-root').dataset.changes);
    if (state !== 'shown') fail(id + ': the comparison state is ' + state);
    await frames();
  };
  const drawn = () => page.evaluate(() => {
    const root = document.querySelector('annotate-root').shadowRoot;
    // A block by its tag and its first text: "div:1" for the first card.
    const label = (n) => {
      const first = document.createTreeWalker(n, NodeFilter.SHOW_TEXT,
        { acceptNode: (t) => (t.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP) }).nextNode();
      return n.localName + ':' + (first ? first.data.trim() : '');
    };
    return {
      news: [...document.querySelectorAll('.annotate-new')].map(label),
      changed: [...document.querySelectorAll('.annotate-changed')].map(label),
      dim: [...document.querySelectorAll('.annotate-dim')].map(label),
      tags: [...root.querySelectorAll('.chg-tag')].map((t) => t.textContent),
      counts: [...root.querySelectorAll('.chg-counts div')].map((d) => d.textContent).join(','),
      rows: [...root.querySelectorAll('.chg-list li')].map((li) => li.textContent),
      nav: root.querySelector('.chg-nav span').textContent,
      bar: root.querySelector('.counts').textContent,
    };
  });
  // No tag covers another tag, nor any text of the page (its own block's first),
  // and each mark has its own tag, at the top left of its block.
  const overlaps = () => page.evaluate(() => {
    const root = document.querySelector('annotate-root').shadowRoot;
    const tags = [...root.querySelectorAll('.chg-tag')]
      .filter((t) => t.style.display !== 'none').map((t) => t.getBoundingClientRect());
    const cross = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
      Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
    const found = [];
    tags.forEach((a, i) => tags.slice(i + 1).forEach((b) => { if (cross(a, b)) found.push('tag on tag'); }));
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.data.trim() || n.parentElement.closest('style,script')) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const rect of range.getClientRects()) {
        if (tags.some((t) => cross(t, rect))) found.push('tag on "' + n.data.trim().slice(0, 24) + '"');
      }
    }
    // Each mark has its tag at the top of its block, above its text: on the
    // top edge of a padded block (not wholly above it, on the block before, as
    // in 0.4.0), just above a block without padding; on its left part.
    for (const block of document.querySelectorAll('.annotate-new, .annotate-changed')) {
      const b = block.getBoundingClientRect();
      const s = getComputedStyle(block);
      const room = parseFloat(s.borderTopWidth) + parseFloat(s.paddingTop);
      const low = room > 2 ? b.top + 1 : b.top - 2;
      // A tag may have slid right past the one before it, on the same row, or
      // gone to the right end of its block when the line above is taken there.
      const slid = (t) => tags.some((o) => o !== t && Math.abs(o.top - t.top) < 17 &&
        t.left >= o.right && t.left <= o.right + 6);
      const atEnd = (t) => t.right <= b.right && t.right >= b.right - 10;
      const own = tags.some((t) => t.left >= b.left - 1 &&
        (t.left <= b.left + Math.min(30, b.width / 2) || slid(t) || atEnd(t)) &&
        t.bottom > low && t.bottom <= b.top + room + 0.5);
      if (!own) found.push('no tag on the top edge of "' + block.textContent.trim().slice(0, 24) + '"');
    }
    return { tags: tags.length, found };
  });
  // The section only, the floating bar hidden: it covers the right column.
  const shoot = async (name, from, to, withBar = false) => {
    const clip = await page.evaluate(([a, b, bar]) => {
      document.querySelector('annotate-root').shadowRoot.querySelector('.bar').style.visibility = bar ? '' : 'hidden';
      const top = document.querySelector(a).getBoundingClientRect().top + scrollY - 34;
      const bottom = document.querySelector(b).getBoundingClientRect().bottom + scrollY + 20;
      return { x: 0, y: Math.max(0, top), width: document.documentElement.clientWidth, height: bottom - top };
    }, [from, to, withBar]);
    await page.screenshot({ path: join(outDir, name), fullPage: true, clip });
    await page.evaluate(() => { document.querySelector('annotate-root').shadowRoot.querySelector('.bar').style.visibility = ''; });
  };

  // 11. The capture of 2026-10-14: a new section of three cards, each card a
  // new block holding a new h3 and two new paragraphs. 4 marks, not 13.
  await settled(nestedId);
  const nest = await drawn();
  const expectNews = ['h2:Planned improvements', 'div:1', 'div:2', 'div:3'];
  if (nest.news.join('|') !== expectNews.join('|')) fail('nested new: ' + JSON.stringify(nest.news));
  if (nest.changed.length || nest.dim.length) fail('nested changed or dim: ' + JSON.stringify(nest));
  if (await page.evaluate(() => document.querySelectorAll('.card h3[class*="annotate-"], .card p[class*="annotate-"]').length)) {
    fail('a block inside a new card is marked');
  }
  if (nest.tags.join(',') !== 'NEW,NEW,NEW,NEW') fail('nested tags: ' + nest.tags.join(','));
  if (nest.counts !== '4new,0changed,0removed' || nest.rows.length !== 4 || nest.nav !== '– / 4' ||
      !nest.bar.includes(' · 4 changes')) fail('nested panel: ' + JSON.stringify(nest));
  // A card's own text is its number: its row reads by all its text.
  if (!nest.rows[1].includes('1 Offline reading Pages stay')) fail('card row: ' + nest.rows[1]);
  // The tint lies over a marked block's own background, which stays (light: #f8fafc).
  const cardLook = await page.evaluate(() => {
    const s = getComputedStyle(document.querySelector('.card'));
    return s.backgroundColor + ' | ' + (s.boxShadow.includes('inset') ? 'tinted' : 'not tinted');
  });
  if (cardLook !== 'rgb(248, 250, 252) | tinted') fail('a marked card lost its background: ' + cardLook);
  const nestGeo = await overlaps();
  if (nestGeo.tags !== 4 || nestGeo.found.length) fail('nested tags placed: ' + JSON.stringify(nestGeo));
  step('nested: 4 marks (h2 + 3 cards), nothing inside a card, panel 4 new');
  await shoot('nested-panel.png', 'main > h2:nth-of-type(2)', '.grid', true);
  await shoot('nested-light.png', 'main > h2:nth-of-type(2)', '.grid');
  await page.emulateMedia({ colorScheme: 'dark' });
  await shoot('nested-dark.png', 'main > h2:nth-of-type(2)', '.grid');
  await page.setViewportSize({ width: 375, height: 800 });
  await frames();
  const phoneGeo = await overlaps();
  if (phoneGeo.tags !== 4 || phoneGeo.found.length) fail('nested tags placed at 375 px: ' + JSON.stringify(phoneGeo));
  await shoot('nested-phone-dark.png', 'main > h2:nth-of-type(2)', '.grid');
  await page.emulateMedia({ colorScheme: 'light' });
  await shoot('nested-phone.png', 'main > h2:nth-of-type(2)', '.grid');
  await page.setViewportSize({ width: 1100, height: 800 });
  step('nested: no tag on a tag or on text, at 1100 and 375 px');
  const stops = [];
  for (let i = 0; i < 5; i++) {
    const at = await nav('▼');
    const flashed = await page.evaluate(() => {
      const n = document.querySelector('.annotate-flash');
      return n && n.classList.contains('annotate-new') ? n.localName : 'not a drawn mark';
    });
    stops.push(at + ' ' + flashed);
  }
  if (stops.join('|') !== '1 / 4 h2|2 / 4 div|3 / 4 div|4 / 4 div|1 / 4 h2') fail('nested navigator: ' + stops.join('|'));
  step('nested: ▼ stops on the 4 marks only: ' + stops.join(' → '));

  // 12. Mixed: CHANGED above NEW keeps both, CHANGED above CHANGED keeps both,
  // NEW above CHANGED keeps the NEW only (and no highlighted word under it).
  await settled(mixedId);
  const mix = await drawn();
  if (mix.changed.join('|') !== 'div:Status: final for review|div:Budget: twelve units per week|' +
      'h3:Budget: twelve units per week') {
    fail('mixed changed: ' + JSON.stringify(mix.changed));
  }
  if (mix.news.join('|') !== 'p:A paragraph added inside a changed block.|div:Extended this week') {
    fail('mixed new: ' + JSON.stringify(mix.news));
  }
  const status = await page.evaluate(() => {
    const box = document.querySelector('main > div');
    return box.querySelectorAll('.annotate-new,.annotate-changed').length +
      (box.matches('.annotate-new,.annotate-changed') ? 1 : 0);
  });
  if (status !== 2) fail('the changed block with a new paragraph carries ' + status + ' marks');
  // The unchanged paragraph of a changed block is dimmed (the witness that the
  // dimming ran); the absorbed changed paragraph under a new block is not.
  if (mix.dim.join('|') !== 'p:The plan holds for the quarter.') fail('mixed dim: ' + JSON.stringify(mix.dim));
  const mixWords = await page.evaluate(() => {
    const h = CSS.highlights && CSS.highlights.get('annotate-added');
    return h ? [...h].map((r) => r.toString()).sort().join(',') : null;
  });
  if (mixWords !== 'beta,final,twelve') fail('mixed highlighted words: ' + mixWords);
  if (mix.tags.slice().sort().join(',') !== 'CHANGED,CHANGED,CHANGED,NEW,NEW' ||
      mix.counts !== '2new,3changed,0removed' || mix.rows.length !== 5 || mix.nav !== '– / 5' ||
      !mix.bar.includes(' · 5 changes')) fail('mixed panel: ' + JSON.stringify(mix));
  const mixGeo = await overlaps();
  if (mixGeo.tags !== 5 || mixGeo.found.length) fail('mixed tags placed: ' + JSON.stringify(mixGeo));
  step('mixed: CHANGED + NEW, CHANGED + CHANGED, NEW alone over a changed block; words: ' + mixWords);
  await shoot('mixed-light.png', 'main > h2:nth-of-type(1)', 'main > div:nth-of-type(3)');
  await page.emulateMedia({ colorScheme: 'dark' });
  await shoot('mixed-dark.png', 'main > h2:nth-of-type(1)', 'main > div:nth-of-type(3)');
  await page.setViewportSize({ width: 375, height: 800 });
  await frames();
  const mixPhone = await overlaps();
  if (mixPhone.tags !== 5 || mixPhone.found.length) fail('mixed tags placed at 375 px: ' + JSON.stringify(mixPhone));
  await shoot('mixed-phone-dark.png', 'main > h2:nth-of-type(1)', 'main > div:nth-of-type(3)');
  await page.emulateMedia({ colorScheme: 'light' });
  await shoot('mixed-phone.png', 'main > h2:nth-of-type(1)', 'main > div:nth-of-type(3)');
  step('mixed: no tag on a tag or on text, at 1100 and 375 px');

  // 13. A whole new list or table is one part; a new item in an old list is its
  // own; a lone new paragraph in a new wrapper keeps the mark; a new section
  // whose h2 sits beside a grid of cards splits into them.
  await settled(listsId);
  const lists = await drawn();
  const expectLists = ['ul:Alpha item to do.', 'li:Third step, added.', 'table:Name',
    'p:A lone new paragraph in a new wrapper.', 'h2:Wrapped part', 'div:1', 'div:2'];
  if (lists.news.join('|') !== expectLists.join('|') || lists.changed.length ||
      lists.counts !== '7new,0changed,0removed' || lists.nav !== '– / 7' || !lists.bar.includes(' · 7 changes')) {
    fail('lists: ' + JSON.stringify(lists));
  }
  const inner = await page.evaluate(() => document.querySelectorAll(
    '.annotate-new [class*="annotate-"], div.note.annotate-new').length);
  if (inner) fail(inner + ' marks inside a new list, table or card, or on the wrapper');
  const listGeo = await overlaps();
  if (listGeo.tags !== 7 || listGeo.found.length) fail('lists tags placed: ' + JSON.stringify(listGeo));
  await shoot('lists-light.png', 'main > h2:nth-of-type(1)', 'main > section');
  await page.emulateMedia({ colorScheme: 'dark' });
  await shoot('lists-dark.png', 'main > h2:nth-of-type(1)', 'main > section');
  await page.emulateMedia({ colorScheme: 'light' });
  step('lists: one mark per new list, table and card, one on the new item of an old list');

  // 14. The cards of the capture: sibling blocks in an <article>, which is no
  // block. A new card is one part: the h2 and one mark per card, none inside.
  await settled(siblingsId);
  const sib = await drawn();
  if (sib.news.join('|') !== 'h2:Planned improvements|article:1|article:2' || sib.changed.length ||
      sib.counts !== '3new,0changed,0removed' || sib.nav !== '– / 3' || !sib.bar.includes(' · 3 changes')) {
    fail('sibling cards: ' + JSON.stringify(sib));
  }
  if (await page.evaluate(() => document.querySelectorAll('article [class*="annotate-"]').length)) {
    fail('a block inside a new card is marked');
  }
  const dotLook = await page.evaluate(() => getComputedStyle(document.querySelector('.dot')).backgroundColor +
    ' | ' + getComputedStyle(document.querySelector('article')).backgroundColor);
  if (dotLook !== 'rgb(14, 116, 144) | rgb(248, 250, 252)') fail('backgrounds under a mark: ' + dotLook);
  for (const width of [1100, 375]) {
    await page.setViewportSize({ width, height: 800 });
    await frames();
    const geo = await overlaps();
    if (geo.tags !== 3 || geo.found.length) fail('sibling tags at ' + width + ' px: ' + JSON.stringify(geo));
  }
  await shoot('siblings-phone.png', 'main > h2:nth-of-type(2)', '.grid');
  await page.setViewportSize({ width: 1100, height: 800 });
  await frames();
  await shoot('siblings-light.png', 'main > h2:nth-of-type(2)', '.grid');
  step('siblings: h2 + one mark per <article> card, none inside, dot and card keep their colour');
  if (errors.length) fail('page errors: ' + errors.join(' | '));
} finally {
  await browser.close();
}
