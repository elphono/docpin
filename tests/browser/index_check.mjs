// Drives the index page in a real headless Chromium: every tray control, and
// attaching a document to a session through the picker.
//
//   node index_check.mjs <playwright-dir> <base-url> <out-dir> <folder-to-add>
//
// The Python side (tests/test_browser.py) prepares five documents in three
// groups (an open session, another session, no session), the conversations
// the picker offers, and a folder holding a sixth document; it checks what
// reached the registry, the disk, the tracked folders and the daemon.
// Screenshots of the full page are written to <out-dir>, light and dark, at
// desktop and phone width, before anything is changed.
import { createRequire } from 'node:module';
import { join } from 'node:path';

const [pwDir, base, outDir, folder] = process.argv.slice(2);
const require = createRequire(join(pwDir, 'package.json'));
const { chromium } = require('playwright');

const fail = (msg) => { console.error('FAIL ' + msg); process.exit(1); };
const step = (msg) => console.log('OK   ' + msg);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const dialogs = [];
  page.on('dialog', async (d) => { dialogs.push(d.message()); await d.accept(); });
  await page.goto(base + '/');
  await page.waitForSelector('section .doc');
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 8);

  const heads = await page.locator('#groups .group h3').allTextContents();
  if (heads.length !== 4 || !heads.includes('Live session') || !heads.includes('Other session') ||
      !heads.includes('session s-untitl') || heads[3] !== 'documents without a known session') {
    fail('groups: ' + JSON.stringify(heads));
  }
  step('grouped by session, unknown last: ' + JSON.stringify(heads));
  const group = (title) => page.locator('#groups .group', { has: page.locator('h3', { hasText: title }) });
  // Sessions gathered by repository: Live session and the untitled one (run
  // in `repo/sub`) in the git repository `repo`, Other session in `other`, a
  // folder no repository holds; the documents without a session after both,
  // in no repository section. Registered within the same second, the groups
  // may come in either order: the comparison is sorted.
  const repos = (await page.locator('#groups > .repo').evaluateAll((nodes) => nodes.map((n) => [
    n.querySelector('h2').textContent, n.querySelector('.repo-kicker').textContent,
    [...n.querySelectorAll('.group h3')].map((h) => h.textContent).sort()]))).sort();
  if (JSON.stringify(repos) !== JSON.stringify([['other', 'Folder', ['Other session']],
    ['repo', 'Repository', ['Live session', 'session s-untitl']]])) {
    fail('repository sections: ' + JSON.stringify(repos));
  }
  const chips = async (title) => group(title).locator('.chip').allTextContents();
  if (!(await chips('session s-untitl')).includes('sub') ||
      (await chips('Live session')).some((text) => text.includes('repo'))) {
    fail('folder chips: ' + JSON.stringify([await chips('session s-untitl'), await chips('Live session')]));
  }
  const last = await page.locator('#groups > *').last().evaluate((n) => n.matches('.group.unknown'));
  if (!last) fail('the documents without a session are not last, outside the repositories');
  step('sessions gathered by repository: ' + JSON.stringify(repos));
  const row = (title) => page.locator('.doc', { hasText: title });
  const kicker = async (title) => (await group(title).locator('.group-kicker').textContent()).trim();
  if (await kicker('Live session') !== 'Open session' ||
      !(await group('Live session').evaluate((node) => node.classList.contains('open')))) {
    fail('Live session not marked open');
  }
  if (await kicker('Other session') !== 'Session' ||
      await group('Other session').evaluate((node) => node.classList.contains('open'))) {
    fail('Other session shown open');
  }
  step('the open session is marked open, the other one is not');
  if (!(await row('Alpha').locator('.badge.live').count())) fail('no listening badge on Alpha');
  const classes = {};
  for (const title of ['Alpha', 'Charlie', 'Echo', 'Foxtrot']) classes[title] = await row(title).getAttribute('class');
  if (!classes.Alpha.includes('pending') || !classes.Echo.includes('delivered') ||
      !classes.Foxtrot.includes('missing') || classes.Charlie !== 'doc') fail('row states: ' + JSON.stringify(classes));
  if ((await row('Alpha').locator('.badge.accent').textContent()) !== '1 note to send') fail('pending badge');
  if (!(await row('Foxtrot').locator('.badge.danger').count())) fail('no missing-file badge');
  step('states at a glance: ' + JSON.stringify(classes));
  const stats = await page.locator('#overview .value').allTextContents();
  if (JSON.stringify(stats) !== '["8","1","1"]') fail('overview: ' + JSON.stringify(stats));
  step('overview: documents, notes to send, sessions open = ' + stats.join(', '));
  if ((await row('Alpha').getByRole('button', { name: 'Send to session' }).getAttribute('class')) !== 'primary' ||
      (await row('Charlie').getByRole('button', { name: 'Send to session' }).getAttribute('class')) === 'primary') {
    fail('Send to session is the main action only where notes wait');
  }
  step('the main action stands out only where notes wait');

  // Screenshots with every kind of row, before anything changes.
  const fits = async (width) => {
    const wide = await page.evaluate(() => document.documentElement.scrollWidth);
    if (wide > width) fail('horizontal scroll at ' + width + ' px: ' + wide);
  };
  await page.screenshot({ path: join(outDir, 'index-light.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-dark.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await fits(390);
  await page.screenshot({ path: join(outDir, 'index-phone-dark.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.screenshot({ path: join(outDir, 'index-phone.png'), fullPage: true });
  await page.setViewportSize({ width: 1100, height: 900 });
  step('screenshots taken; fits at 390 px');

  // The filter and the "Notes to send" view.
  await page.locator('#filter').fill('echo');
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 1);
  await page.locator('#filter').fill('');
  await page.locator('#show-pending').click();
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 1 &&
    document.querySelector('.doc').textContent.includes('Alpha'));
  await page.locator('#show-all').click();
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 8);
  step('filter by text, and "Notes to send" shows Alpha alone');

  // Attach Bravo, which has no known session, through the picker.
  await row('Bravo').getByRole('button', { name: 'Attach to session…' }).click();
  await page.waitForSelector('#picker[open] .option');
  const options = page.locator('#picker .option');
  const names = await options.locator('.name').allTextContents();
  if (JSON.stringify(names) !== '["Live session","Other session","Untitled session"]') {
    fail('picker: open first, then most recent, never a subagent nor an unresumable one: ' + JSON.stringify(names));
  }
  if (!(await options.nth(0).locator('.badge.live').count())) fail('no open badge in the picker');
  step('picker lists ' + JSON.stringify(names));
  if (await page.evaluate(() => document.activeElement.id) !== 'picker-search') fail('the search field has no focus');
  if (await page.locator('#picker-detach').isVisible()) fail('Detach offered for a document without a session');
  await page.screenshot({ path: join(outDir, 'index-picker.png') });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-picker-dark.png') });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 390, height: 844 });
  await fits(390);
  await page.screenshot({ path: join(outDir, 'index-picker-phone.png') });
  await page.setViewportSize({ width: 1100, height: 900 });
  const typed = async (text, want) => {
    await page.locator('#picker-search').fill(text);
    const got = await options.locator('.name').allTextContents();
    if (JSON.stringify(got) !== JSON.stringify(want)) fail('typing ' + text + ': ' + JSON.stringify(got));
  };
  await typed('third', ['Untitled session']);          // its folder
  await typed('s-unt', ['Untitled session']);          // the start of its id
  await typed('untitled-zzz', []);
  await typed('other', ['Other session']);             // its title
  step('the picker filters on folder, start of id and title');
  await page.locator('#picker-search').press('Enter');
  await page.waitForFunction(() => !document.getElementById('picker').open);
  await page.waitForFunction(() => [...document.querySelectorAll('#groups .group')].some((s) =>
    s.querySelector('h3').textContent === 'Other session' && s.textContent.includes('Bravo')));
  const attached = await page.locator('#message').textContent();
  if (!attached.startsWith('Bravo attached to Other session')) fail('attach message: ' + attached);
  step('Enter attached Bravo, which moved to Other session: ' + attached);

  // From the More menu: the current session is marked; Esc closes the picker.
  await row('Charlie').locator('summary.more-toggle').click();
  await row('Charlie').getByRole('button', { name: 'Attach to session…' }).click();
  await page.waitForSelector('#picker[open] .option');
  const current = await page.locator('#picker .option', { has: page.locator('.badge', { hasText: 'current' }) })
    .locator('.name').allTextContents();
  if (JSON.stringify(current) !== '["Live session"]') fail('current session: ' + JSON.stringify(current));
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.getElementById('picker').open);
  if (await group('Live session').locator('.doc', { hasText: 'Charlie' }).count() !== 1) fail('Esc changed something');
  step('the picker marks the current session, Esc closes it and changes nothing');

  await row('Charlie').locator('summary.more-toggle').click();
  await row('Charlie').getByRole('button', { name: 'Attach to session…' }).click();
  await page.waitForSelector('#picker[open] .option');
  await page.locator('#picker-detach').click();
  await page.waitForFunction(() => !document.getElementById('picker').open);
  await page.waitForFunction(() => [...document.querySelectorAll('#groups .group')].some((s) =>
    s.querySelector('h3').textContent === 'documents without a known session' && s.textContent.includes('Charlie')));
  step('Detach from the picker: ' + (await page.locator('#message').textContent()));

  await row('Bravo').locator('summary.more-toggle').click();
  await row('Bravo').getByRole('button', { name: 'Detach' }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('#groups .group')].some((s) =>
    s.querySelector('h3').textContent === 'documents without a known session' && s.textContent.includes('Bravo')));
  step('Detach: ' + (await page.locator('#message').textContent()));

  await row('Alpha').getByRole('button', { name: 'Send to session' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.includes('delivered to the open session'));
  if (!(await page.locator('#toast.show').count())) fail('the answer of an action is not shown');
  step('send: ' + (await page.locator('#message').textContent()));

  await row('Bravo').locator('summary.more-toggle').click();
  await row('Bravo').getByRole('button', { name: 'Unmanage' }).click();
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Bravo'));
  step('unmanage: row gone after confirm');

  await row('Charlie').locator('summary.more-toggle').click();
  await row('Charlie').getByRole('button', { name: 'Delete file…' }).click();
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Charlie'));
  step('delete: row gone after confirm');

  const tracked = await page.locator('#folder-list .tracked').allTextContents();
  if (tracked.length !== 1 || !tracked[0].includes('only under a docs/ folder')) {
    fail('default folder: ' + JSON.stringify(tracked));
  }
  step('default folder shown: ' + tracked[0]);
  const parent = folder.slice(0, folder.lastIndexOf('/') + 1);
  await page.locator('#folder-input').fill(parent);
  await page.waitForFunction((want) => [...document.querySelectorAll('#folder-suggest option')]
    .some((o) => o.value === want), folder + '/');
  step('subfolders proposed while typing');
  await page.locator('#folder-input').fill(folder);
  await page.getByRole('button', { name: 'Add folder' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Tracked:'));
  await page.waitForFunction(() => document.getElementById('scan-state').textContent.includes('1 document(s) added'));
  await page.waitForFunction(() => document.getElementById('groups').textContent.includes('Delta'));
  step('folder added, its recent document listed: ' + (await page.locator('#scan-state').textContent()));
  await page.screenshot({ path: join(outDir, 'index-folders.png'), fullPage: true });

  await page.locator('.tracked', { hasText: 'every .html' }).getByRole('button', { name: 'Remove' }).click();
  await page.waitForFunction(() => document.querySelectorAll('#folder-list .tracked').length === 1);
  step('folder removed after confirm');

  // Excluded folders (2026-10-19): Delta's folder, no longer tracked, still
  // holds a listed document; excluding it unmanages that document.
  if (await page.locator('#excluded-list .tracked').count()) fail('an excluded folder before any');
  await page.locator('#exclude-input').fill(parent);
  await page.waitForFunction((want) => [...document.querySelectorAll('#folder-suggest option')]
    .some((o) => o.value === want), folder + '/');
  step('the folder to exclude is offered the same subfolders');
  await page.locator('#exclude-input').fill(folder);
  await page.getByRole('button', { name: 'Exclude', exact: true }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Excluded:'));
  const excluded = await page.locator('#message').textContent();
  if (!excluded.includes('1 document(s) under it unmanaged')) fail('exclusion: ' + excluded);
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Delta'));
  const excludedRows = await page.locator('#excluded-list .tracked').allTextContents();
  if (excludedRows.length !== 1 || !excludedRows[0].includes('nothing under it is a document')) {
    fail('excluded list: ' + JSON.stringify(excludedRows));
  }
  await page.locator('#folders').screenshot({ path: join(outDir, 'index-excluded.png') });
  step('folder excluded after confirm, its listed document unmanaged: ' + excluded);
  await page.locator('#excluded-list .tracked').getByRole('button', { name: 'Remove' }).click();
  await page.waitForFunction(() => !document.querySelectorAll('#excluded-list .tracked').length &&
    document.getElementById('message').textContent.startsWith('No longer excluded'));
  step('exclusion removed: ' + (await page.locator('#message').textContent()));

  // Cleanup files in every group (2026-10-20), and in a session's group it
  // takes the documents of THAT session only: Foxtrot, in the untitled
  // session, whose file is missing (never protected), goes; nothing of
  // another group moves, though Juliet and Golf could be taken.
  const groupCount = await page.locator('#groups .group').count();
  const buttons = await page.locator('#groups .group > .group-head .group-tools .cleanup').count();
  if (groupCount !== 4 || buttons !== groupCount) fail('Cleanup files per group: ' + buttons + '/' + groupCount);
  const idsBefore = await page.evaluate(() => [...document.querySelectorAll('#groups .doc')].map((n) => n.dataset.id));
  const foxtrotId = await row('Foxtrot').getAttribute('data-id');
  await group('Other session').screenshot({ path: join(outDir, 'index-session-group.png') });
  const untitled = group('session s-untitl');
  await untitled.getByRole('button', { name: 'Cleanup files' }).click();
  await page.waitForSelector('#cleanup[open]');
  const sessionDialog = await page.evaluate(() => ['cleanup-title', 'cleanup-text', 'cleanup-kept', 'cleanup-confirm']
    .map((id) => document.getElementById(id).textContent));
  const sessionItems = await page.locator('#cleanup-list li').allTextContents();
  if (sessionDialog[0] !== 'Clean up the documents of session s-untitl' ||
      sessionDialog[1] !== '1 document leaves the list now, with its notes. 1 file is already missing: ' +
        'nothing to delete there.' || sessionDialog[2] !== '' ||
      sessionDialog[3] !== 'Unmanage 1 document' || sessionItems.length !== 1 ||
      !sessionItems[0].startsWith('Foxtrot ') || !sessionItems[0].endsWith('file missing')) {
    fail('session cleanup confirmation: ' + JSON.stringify([sessionDialog, sessionItems]));
  }
  step('a session group asks first: ' + sessionDialog[0] + ' | ' + sessionItems[0]);
  await page.evaluate(() => document.getElementById('toast').classList.remove('show'));
  await page.screenshot({ path: join(outDir, 'index-cleanup-session.png') });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-cleanup-session-dark.png') });
  await page.setViewportSize({ width: 375, height: 812 });
  await fits(375);
  await page.screenshot({ path: join(outDir, 'index-cleanup-session-phone.png') });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.locator('#cleanup-confirm').click();
  await page.waitForFunction(() => !document.getElementById('cleanup').open &&
    document.getElementById('message').textContent === '1 document(s) of the session s-untitl unmanaged; ' +
      '1 had no file any more.');
  await page.waitForFunction((gone) => !document.querySelector('#groups .doc[data-id="' + gone + '"]'), foxtrotId);
  const idsAfter = await page.evaluate(() => [...document.querySelectorAll('#groups .doc')].map((n) => n.dataset.id));
  const expected = idsBefore.filter((id) => id !== foxtrotId).sort();
  if (JSON.stringify(idsAfter.sort()) !== JSON.stringify(expected)) {
    fail('a session cleanup touched another group: ' + JSON.stringify([idsBefore, idsAfter]));
  }
  step('session cleanup: ' + (await page.locator('#message').textContent()) + '; every other document stays');

  await page.locator('#days').selectOption('30');
  await page.getByRole('button', { name: 'Rescan all folders' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Rescan started'));
  // A rescan also clears (2026-10-19): Juliet, 40 days old, leaves the list;
  // Echo, as old, is delivered (the session is on it): protected.
  await page.waitForFunction(() => document.getElementById('scan-state').textContent
    .includes('0 document(s) added, 1 older one(s) removed (1 kept'));
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Juliet'));
  if (await page.locator('.doc', { hasText: 'Bravo' }).count()) fail('the rescan brought back an unmanaged document');
  if (!(await page.locator('.doc', { hasText: 'Echo' }).count())) fail('the rescan took a protected document');
  step('rescan: ' + (await page.locator('#scan-state').textContent()));

  // Folding (2026-10-19): a session group (its chevron) and the folder
  // section that holds it (its header) fold, keep their count, and stay
  // folded across a redraw and a reload; unfolded, nothing is left in the
  // storage. The group folds first: once its section is folded, it is hidden.
  const other = group('Other session');
  const repoBox = page.locator('#groups > .repo', { has: page.locator('h2', { hasText: /^other$/ }) });
  const isFolded = (loc) => loc.evaluate((n) => n.classList.contains('folded'));
  const counts = async () => (await other.locator('.group-meta .chip', { hasText: 'document' }).textContent()).trim()
    + ' | ' + (await repoBox.locator('.repo-facts').textContent()).trim();
  const open = await counts();
  if (!open.startsWith('1 document | ') || !/^1 document \| \d+ sessions? · \d+ documents?$/.test(open)) {
    fail('counts before folding: ' + open);
  }
  const foldedState = async () => {
    if (!(await isFolded(other)) || !(await isFolded(repoBox))) fail('not folded any more');
    if (await other.locator('.doc').first().isVisible()) fail('a folded group still shows its documents');
    if (await repoBox.locator('.repo-sessions').isVisible()) fail('a folded repository still shows its sessions');
    const shown = await counts();
    if (shown !== open) fail('folded counts: ' + shown + ', open: ' + open);
    if (await other.locator('.fold-toggle').getAttribute('aria-expanded') !== 'false') fail('aria-expanded');
    return shown;
  };
  await other.locator('.fold-toggle').click();
  await repoBox.locator('.repo-head h2').click();                 // the header itself
  step('folded, the counts stay: ' + (await foldedState()));
  await page.evaluate(() => { document.querySelector('#groups > *').dataset.before = 'redraw'; });
  await page.locator('#filter').fill('o');
  await page.locator('#filter').fill('');
  await page.waitForFunction(() => !document.querySelector('#groups [data-before]') &&
    document.querySelector('#groups .group'));
  await foldedState();
  step('a redraw keeps them folded');
  await page.evaluate(() => document.getElementById('toast').classList.remove('show'));
  await page.screenshot({ path: join(outDir, 'index-folded.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-folded-dark.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await fits(375);
  await page.screenshot({ path: join(outDir, 'index-folded-phone.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.reload();
  await page.waitForSelector('#groups .group');
  await foldedState();
  step('a reload keeps them folded (localStorage)');
  await repoBox.locator('.repo-head .fold-toggle').click();
  await other.locator('.fold-toggle').click();
  if ((await isFolded(other)) || (await isFolded(repoBox)) ||
      !(await other.locator('.doc', { hasText: 'Echo' }).isVisible())) fail('unfold');
  const kept = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('docpin.folded.')));
  if (kept.length) fail('storage after unfolding: ' + JSON.stringify(kept));
  step('unfolded: the documents are back, nothing left in the storage');

  // Without storage (private window, blocked site data): folding still works.
  const bare = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  bare.on('pageerror', (e) => errors.push('no storage: ' + e.message));
  await bare.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new Error('storage denied'); } });
  });
  await bare.goto(base + '/');
  await bare.waitForSelector('#groups .group');
  const bareOther = bare.locator('#groups .group', { has: bare.locator('h3', { hasText: 'Other session' }) });
  await bareOther.locator('.fold-toggle').click();
  await bare.locator('#filter').fill('o');
  await bare.locator('#filter').fill('');
  if (!(await bareOther.evaluate((n) => n.classList.contains('folded')))) fail('no storage: not folded');
  await bare.close();
  step('without storage, a group folds and stays folded across a redraw');

  // Cleanup files (2026-10-19), in the header of the documents without a
  // session: Golf goes; Hotel, delivered (the session is on it), stays.
  const unknownGroup = page.locator('#groups .group.unknown');
  const cleanupButton = unknownGroup.getByRole('button', { name: 'Cleanup files' });
  if (await cleanupButton.isDisabled()) fail('Cleanup files disabled with a document to clean');
  await unknownGroup.screenshot({ path: join(outDir, 'index-unknown-group.png') });
  await page.setViewportSize({ width: 375, height: 812 });
  await unknownGroup.screenshot({ path: join(outDir, 'index-unknown-group-phone.png') });
  await page.setViewportSize({ width: 1100, height: 900 });
  await cleanupButton.click();
  await page.waitForSelector('#cleanup[open]');
  if (await isFolded(unknownGroup)) fail('the Cleanup button folded the group');
  const cleanupText = await page.locator('#cleanup-text').textContent();
  const cleanupItems = await page.locator('#cleanup-list li').allTextContents();
  const cleanupKept = await page.locator('#cleanup-kept').textContent();
  const cleanupLabel = await page.locator('#cleanup-confirm').textContent();
  if (cleanupText !== '1 document leaves the list now, with its notes. Its file stays on disk for 30 ' +
        'days, then the daemon deletes it, unless written again or listed again before.' ||
      JSON.stringify(cleanupItems) !== '["Golf docs/g.html"]' || cleanupLabel !== 'Unmanage 1 document' ||
      cleanupKept !== '1 protected document stays: a session listens or is on it, or notes wait to be sent.') {
    fail('cleanup confirmation: ' + JSON.stringify([cleanupText, cleanupItems, cleanupKept, cleanupLabel]));
  }
  step('Cleanup files asks first, with the count: ' + cleanupLabel + ' | ' + cleanupKept);
  await page.screenshot({ path: join(outDir, 'index-cleanup.png') });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-cleanup-dark.png') });
  await page.setViewportSize({ width: 375, height: 812 });
  await fits(375);
  await page.screenshot({ path: join(outDir, 'index-cleanup-phone.png') });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.locator('#cleanup-cancel').click();
  await page.waitForFunction(() => !document.getElementById('cleanup').open);
  if (!(await page.locator('.doc', { hasText: 'Golf' }).count())) fail('Cancel cleaned up');
  step('Cancel changes nothing');
  await cleanupButton.click();
  await page.waitForSelector('#cleanup[open]');
  await page.locator('#cleanup-confirm').click();
  await page.waitForFunction(() => !document.getElementById('cleanup').open &&
    document.getElementById('message').textContent.startsWith('1 document(s) without a known session unmanaged'));
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Golf'));
  if (!(await page.locator('.doc', { hasText: 'Hotel' }).count())) fail('Cleanup took a protected document');
  if (!(await cleanupButton.isDisabled())) fail('Cleanup files still offered with nothing to clean');
  step('Cleanup files: ' + (await page.locator('#message').textContent()));

  await page.getByRole('button', { name: 'Restart daemon' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Restart asked'));
  await page.getByRole('button', { name: 'Stop daemon' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Stop asked'));
  step('daemon buttons answered; dialogs: ' + dialogs.length);
  if (dialogs.length !== 5) {
    fail('expected 5 confirmations (unmanage, delete, remove folder, exclude folder, stop), got ' +
         dialogs.length);
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await fits(390);
  step('still fits at 390 px');

  // A daemon up to date shows no banner; one older than the page (it says
  // neither `stale` nor any repository) gets the banner, and the sessions
  // listed flat rather than under made-up folders.
  if (await page.locator('#stale').isVisible()) fail('stale banner shown by a fresh daemon');
  const old = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  old.on('pageerror', (e) => errors.push(e.message));
  await old.route('**/api/docs', async (route) => {
    const data = await (await route.fetch()).json();
    delete data.stale;
    for (const group of data.sessions) {
      for (const field of ['repo', 'repo_label', 'is_repo']) delete group[field];
    }
    await route.fulfill({ json: data });
  });
  await old.goto(base + '/');
  await old.waitForSelector('#groups .group');
  if (!(await old.locator('#stale').isVisible())) fail('no stale banner for an older daemon');
  if (await old.locator('#groups .repo').count()) fail('repository sections made up for an older daemon');
  if (!(await old.locator('#groups > .group').count())) fail('no session listed for an older daemon');
  await old.close();
  step('an older daemon: banner shown, sessions listed flat');

  if (errors.length) fail('page errors: ' + errors.join(' | '));
  step('no page error');
} finally {
  await browser.close();
}
