// Past notes in a real headless Chromium: hidden until asked for, gone with
// their passage, and a pin only ever on its own passage.
//
//   node notes_check.mjs <playwright-dir> <base-url> <doc-id> <out-dir>
//
// The Python side (tests/test_browser.py) seeded the notes: 1 to 4 sent (1 on
// a kept passage, 2 on a rewritten one, 3 on a deleted one, 4 in a closed zoom),
// 5 to 10 unsent (5 on the rewritten one, 6 and 9 in the zoom, 7 in a closed
// <details>, 8 on a deleted passage, 10 in a hidden tab, 11 in a scrolling table
// of a closed <details> of the zoom). It checks afterwards
// that all survive.
import { createRequire } from 'node:module';
import { join } from 'node:path';

const [pwDir, base, docId, outDir] = process.argv.slice(2);
const require = createRequire(join(pwDir, 'package.json'));
const { chromium } = require('playwright');

const fail = (msg) => { console.error('FAIL ' + msg); process.exit(1); };
const step = (msg) => console.log('OK   ' + msg);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const load = async () => {
    await page.goto(`${base}/docs/${docId}`);
    await page.waitForFunction(() => {
      const host = document.querySelector('annotate-root');
      return host && host.dataset.changes && host.dataset.changes !== 'pending' &&
        host.shadowRoot.querySelector('.bar').textContent.includes('to send');
    });
  };
  // The numbers of the pins on screen, sorted.
  const shown = () => page.evaluate(() => [...document.querySelector('annotate-root').shadowRoot
    .querySelectorAll('.pin')].filter((p) => p.style.display !== 'none')
    .map((p) => Number(p.textContent.replace('✓', ''))).sort((a, b) => a - b).join(','));
  const expectPins = async (want, why) => {
    await page.waitForFunction((w) => [...document.querySelector('annotate-root').shadowRoot
      .querySelectorAll('.pin')].filter((p) => p.style.display !== 'none')
      .map((p) => Number(p.textContent.replace('✓', ''))).sort((a, b) => a - b).join(',') === w,
    want, { timeout: 1500 }).catch(async () => fail(why + ': pins ' + await shown() + ', expected ' + want));
  };
  // A pin stands on its anchor's box, never elsewhere. It gets there on the
  // render that follows a change, hence the bounded wait.
  const onItsPassage = async (number, selector) => {
    const gapOf = ([n, sel]) => {
      const pin = [...document.querySelector('annotate-root').shadowRoot.querySelectorAll('.pin')]
        .find((p) => p.textContent.replace('✓', '') === String(n)).getBoundingClientRect();
      const box = document.querySelector(sel).getBoundingClientRect();
      const x = pin.left + pin.width / 2;
      const y = pin.top + pin.height / 2;
      return Math.max(box.left - x, x - box.right, box.top - y, y - box.bottom, 0);
    };
    await page.waitForFunction(`(${gapOf})(${JSON.stringify([number, selector])}) <= 1`, null, { timeout: 1500 })
      .catch(async () => fail(`pin ${number} is ${await page.evaluate(gapOf, [number, selector])}px away from ${selector}`));
  };
  const toggle = (text) => page.evaluate(() => {
    const t = document.querySelector('annotate-root').shadowRoot.querySelector('.past-toggle');
    return t ? t.textContent : '';
  }).then((t) => { if (t !== text) fail('past toggle says "' + t + '", expected "' + text + '"'); });
  const clickToggle = () => page.evaluate(() =>
    document.querySelector('annotate-root').shadowRoot.querySelector('.past-toggle').click());

  await load();

  // 1. By default: the unsent notes only, each on its passage. Those in the
  // closed zoom and in the hidden tab have none (the top of the page is where
  // they used to land); the one in the closed <details> sits on it.
  await expectPins('5,7', 'default view');
  await onItsPassage(5, '#rewritten');
  await onItsPassage(7, '#fold');
  step('default view: unsent pins only, none in the closed zoom, none at the top of the page');
  await page.screenshot({ path: join(outDir, 'notes-default.png') });

  // 2. Lost anchors: the unsent one is listed, the sent ones (2: rewritten
  // passage, 3: deleted one) show nowhere and are not counted as past notes.
  const orphans = await page.evaluate(() => [...document.querySelector('annotate-root').shadowRoot
    .querySelectorAll('.orphan-num')].map((n) => n.textContent.trim()).join(','));
  if (orphans !== '#8') fail('listed as lost: ' + orphans);
  await toggle('Show past notes (2)');
  step('lost anchors: the unsent one listed, the sent ones gone, 2 past notes left');

  // 3. Asked for: the past note on a kept passage shows; the one in the zoom
  // waits for it. The note on the rewritten passage does not come back.
  await clickToggle();
  await expectPins('1,5,7', 'past notes shown');
  await onItsPassage(1, '#kept');
  await toggle('Hide past notes (2)');
  step('past notes on request: 1 shows on its passage, 4 waits for its zoom, 2 and 3 stay gone');

  // 4. The choice survives a reload.
  await load();
  await expectPins('1,5,7', 'after a reload');
  step('the choice survives a reload');

  // 5. The zoom opens: its pins show at once, on their passages, and those of
  // the page under it hide; the one scrolled out of the box stays hidden until
  // scrolled to, and then follows.
  await page.locator('#open').click();
  await expectPins('4,6,11', 'zoom open');
  await onItsPassage(4, '#in-zoom');
  await onItsPassage(6, '#in-zoom');
  // A closed section of the zoom holds the pin of a table that scrolls on its
  // own: that table's box lies below the section, and must not clip the pin.
  await onItsPassage(11, '#zoom-fold');
  await page.evaluate(() => { document.querySelector('#zoom-fold').open = true; });
  await expectPins('4,6,11', 'section of the zoom opened');
  await onItsPassage(11, '#in-zoom-fold');
  await page.evaluate(() => { document.querySelector('#zoom-fold').open = false; });
  await page.screenshot({ path: join(outDir, 'notes-zoom.png') });
  await page.evaluate(() => { document.querySelector('.box').scrollTop = 100000; });
  await expectPins('9', 'zoom scrolled to its end');
  await onItsPassage(9, '#far');
  step('zoom open: its pins on their passages, none from the page under it; scrolled out, hidden; scrolled to, shown');
  await page.evaluate(() => { document.querySelector('.box').scrollTop = 0; });
  await page.locator('#close').click();
  await expectPins('1,5,7', 'zoom closed');
  step('zoom closed: its pins are gone again');

  // 6. A note saved from the page writes the whole list: nothing hidden is lost.
  const box = await page.locator('#kept').boundingBox();
  await page.keyboard.down('Alt');
  await page.mouse.click(box.x + 300, box.y + box.height / 2);
  await page.keyboard.up('Alt');
  await page.waitForSelector('.bubble textarea');
  await page.locator('.bubble textarea').fill('A new note.');
  await page.locator('.bubble textarea').press('Enter');
  await page.waitForFunction(async (api) => (await (await fetch(api)).json())
    .annotations.some((a) => a.note === 'A new note.'), `/api/docs/${docId}/annotations`);
  step('a save from the page');

  // 7. Hide again: back to the unsent ones, and three rounds hold.
  for (let i = 0; i < 3; i++) {
    await clickToggle();
    await expectPins(i % 2 ? '1,5,7,12' : '5,7,12', 'toggle round ' + i);
  }
  step('Hide / Show / Hide, three rounds');

  // 8. Phone width, no local storage: the page still works.
  await page.setViewportSize({ width: 390, height: 800 });
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } });
  });
  await load();
  await expectPins('5,7,12', 'no local storage');
  await clickToggle();
  await expectPins('1,5,7,12', 'no local storage, shown');
  step('without local storage: hidden by default, the toggle still works');

  if (errors.length) fail('page errors: ' + errors.join(' | '));
} finally {
  await browser.close();
}
