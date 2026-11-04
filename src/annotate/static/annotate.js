/*
 * annotate overlay: Alt+click on the document pins a numbered note there.
 *
 * Injected by the daemon at the end of <body> (see htmldoc.inject_overlay),
 * with the document id in `data-annotate-doc`. Vanilla JS, no build step.
 *
 * The model is `annotations`: ALWAYS the full list, orphans included. Every
 * change PUTs that whole list, so an annotation whose anchor no longer
 * resolves must stay in the model, or the next save would delete it in
 * silence. An orphan not sent yet is listed in the bar with its quote instead
 * of a pin; an orphan already sent has done its work and shows nowhere.
 *
 * A sent note is history: its pin stays hidden until the reader asks for the
 * past notes (the bar), and a pin only ever sits on its own passage. Whatever
 * holds that passage closed (a zoom, a folded section), the pin waits with it.
 *
 * The UI lives in a shadow root: the documents style `button`, `textarea`
 * and `div` freely, and none of it must reach the bar or the bubble.
 *
 * A pin is anchored to an ELEMENT (stable across edits), which can be much
 * wider than what the note is about: a whole table cell, a whole figure.
 * `at` therefore records the word under the cursor, in context
 * ("… the [[Second]] paragraph, which …"),
 * or, in a figure, the label nearest to the click (user note, 2026-10-05:
 * "the element chosen is too wide to say what the note designates").
 */
(() => {
  'use strict';
  const tag = document.currentScript ||
    document.querySelector('script[data-annotate-doc]');
  const DOC = tag && tag.getAttribute('data-annotate-doc');
  if (!DOC || window.__annotateLoaded) return;
  window.__annotateLoaded = true;

  const API = '/api/docs/' + encodeURIComponent(DOC);
  const HEADERS = { 'Content-Type': 'application/json', 'X-Annotate': 'overlay' };
  const QUOTE_MAX = 120;
  const SELECTION_MAX = 600;
  const AT_SPAN = 40;
  const BLOCKS = 'p,li,td,th,h1,h2,h3,h4,h5,h6,dt,dd,blockquote,figcaption,pre,caption,summary';
  const STATUS_TEXT = {
    new: 'no note yet', annotated: 'notes waiting to be sent',
    delivered: 'notes delivered, the session is on it',
    answered: 'the session edited the document',
  };

  let annotations = [];
  let entry = null;
  let message = '';
  let editing = null;          // { item, isNew }
  let suppressClick = false;
  let collapsed = false;
  let loadedMtime = null;      // the file as this page shows it
  let changed = false;         // the file on disk is newer than the page
  let saving = Promise.resolve();
  let changeView = null;       // what changed since the last send (changes.js)
  const PAST_KEY = 'docpin.pastNotes:' + DOC;
  let showPast = false;        // the sent notes' pins, off unless asked for
  try { showPast = localStorage.getItem(PAST_KEY) === '1'; } catch (err) { showPast = false; }

  // -- DOM scaffolding -----------------------------------------------------
  const host = document.createElement('annotate-root');
  host.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;' +
    'z-index:2147483000;';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = '<link rel="stylesheet" href="/static/annotate.css">' +
    '<div class="layer"></div><div class="bar" role="region" ' +
    'aria-label="annotations"></div>';
  const layer = root.querySelector('.layer');
  const bar = root.querySelector('.bar');
  document.body.appendChild(host);
  // Where the comparison with the last send stands: pending, none, shown or
  // error. The bar renders before it ends, so the bar says nothing about it.
  host.dataset.changes = 'pending';

  const make = (name, cls, text) => {
    const node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const squash = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const isUi = (event) => event.composedPath().includes(host);

  // -- anchoring -------------------------------------------------------------
  function anchorFor(target) {
    let node = target && target.nodeType === 1 ? target : document.body;
    // Inside an SVG figure, anchor to the outermost <svg>: stable, and its
    // box is what the reader sees.
    let svg = node.closest && node.closest('svg');
    while (svg && svg.parentElement && svg.parentElement.closest('svg')) {
      svg = svg.parentElement.closest('svg');
    }
    if (svg) node = svg;
    if (node === document.documentElement) node = document.body;
    return node;
  }

  function selectorFor(node) {
    const parts = [];
    while (node && node.nodeType === 1 && node !== document.body &&
           node !== document.documentElement) {
      if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
        parts.unshift('#' + CSS.escape(node.id));
        return parts.join(' > ');
      }
      let index = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.localName === node.localName) index++;
      }
      parts.unshift(node.localName + ':nth-of-type(' + index + ')');
      node = node.parentElement;
    }
    parts.unshift('body');
    return parts.join(' > ');
  }

  // The word under the cursor between [[ ]], with whole words of context on
  // each side, read in the enclosing block so that <strong>/<code>
  // boundaries do not cut them.
  function textAt(e) {
    let node = null;
    let offset = 0;
    if (document.caretRangeFromPoint) {
      const range = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (range) { node = range.startContainer; offset = range.startOffset; }
    } else if (document.caretPositionFromPoint) {
      const pos = document.caretPositionFromPoint(e.clientX, e.clientY);
      if (pos) { node = pos.offsetNode; offset = pos.offset; }
    }
    if (node && node.nodeType === 3 && onText(node, e)) {
      const block = (node.parentElement.closest(BLOCKS)) || node.parentElement;
      const before = document.createRange();
      before.selectNodeContents(block);
      before.setEnd(node, offset);
      const all = block.textContent;
      const at = before.toString().length;
      let start = at;
      let end = at;
      while (start > 0 && !/\s/.test(all[start - 1])) start--;
      while (end < all.length && !/\s/.test(all[end])) end++;
      let head = all.slice(Math.max(0, start - AT_SPAN), start);
      let tail = all.slice(end, end + AT_SPAN);
      const cutHead = start - AT_SPAN > 0;
      const cutTail = end + AT_SPAN < all.length;
      if (cutHead) head = head.replace(/^\S*\s/, '');   // no half word at the edges
      if (cutTail) tail = tail.replace(/\s\S*$/, '');
      const word = all.slice(start, end);
      return squash((cutHead ? '… ' : '') + head + (word ? '[[' + word + ']]' : '[[ ]]') +
        tail + (cutTail ? ' …' : ''));
    }
    const svg = e.target.closest && e.target.closest('svg');
    if (svg) {
      let best = null;
      let distance = Infinity;
      for (const label of svg.querySelectorAll('text')) {
        const b = label.getBoundingClientRect();
        const dx = Math.max(b.left - e.clientX, 0, e.clientX - b.right);
        const dy = Math.max(b.top - e.clientY, 0, e.clientY - b.bottom);
        const d = Math.hypot(dx, dy);
        if (d < distance && squash(label.textContent)) { distance = d; best = label; }
      }
      if (best) {
        return 'in the figure, ' + (distance === 0 ? 'on' : 'next to') + ' the label "' +
          squash(best.textContent) + '"';
      }
    }
    return '';
  }

  // caretRangeFromPoint snaps to the nearest text even in blank space: the
  // click must be ON the text node's boxes to count.
  function onText(node, e) {
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (e.clientX >= r.left - 2 && e.clientX <= r.right + 2 &&
          e.clientY >= r.top - 2 && e.clientY <= r.bottom + 2) return true;
    }
    return false;
  }

  function resolve(selector) {
    try {
      return selector ? document.querySelector(selector) : null;
    } catch (err) {
      return null;
    }
  }

  // A sent note's passage is gone when its text left the anchor: the selector
  // is a position, and after an edit it can name another element. A quote made
  // from a selection may run past the anchor: it is looked for in the page.
  const flat = (s) => String(s || '').replace(/\s+/g, '').toLowerCase();
  function quoteHolds(item, target) {
    const quote = flat(item.quote);
    if (!quote) return true;
    let node = target;
    if (target.querySelector('annotate-fold')) {
      node = target.cloneNode(true);
      node.querySelectorAll('annotate-fold').forEach((f) => f.remove());
    }
    if (flat(node.textContent).includes(quote) || flat(node.innerText).includes(quote)) return true;
    return !item.at && flat(document.body.textContent).includes(quote);
  }

  // The element a note stands on, or null: its selector resolves nowhere, or
  // it was sent and its passage is gone.
  function anchorOf(item) {
    const target = resolve(item.selector);
    return target && item.sent_at && !quoteHolds(item, target) ? null : target;
  }

  // The box a pin sits on: the anchor's, or the closed <details> that holds it
  // (its content still has a box of its own, measured, below the summary line).
  // Anything else that hides the anchor (a closed zoom, a hidden tab) has no
  // place to offer: the box of a mere ancestor would put the pin far from its
  // passage, at the top of the page for a zoom. Null then.
  function visibleBox(node) {
    const closed = node.closest('details:not([open])');
    const holder = closed && !node.closest('summary') ? closed : node;
    const box = holder.getBoundingClientRect();
    return box.width > 0 || box.height > 0 ? box : null;
  }

  // Inside a zoom, a passage scrolled out of the zoom's box: its pin must not
  // float over the page around it. (In the page itself, the pin scrolls away
  // with its passage.)
  function scrolledOut(target, y) {
    const dialog = target.closest('dialog');
    if (!dialog) return false;
    for (let up = target.parentElement; up; up = up.parentElement) {
      if (getComputedStyle(up).overflowY !== 'visible') {
        const clip = up.getBoundingClientRect();
        if (y < clip.top || y > clip.bottom) return true;
      }
      if (up === dialog) break;
    }
    return false;
  }

  // A passage of the page under an open zoom: the overlay is above every zoom,
  // its pin would float over the zoom's text.
  function behindZoom(target, x, y) {
    return [...document.querySelectorAll('dialog[open]')].some((dialog) => {
      if (dialog.contains(target)) return false;
      const r = dialog.getBoundingClientRect();
      return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    });
  }

  // -- persistence -----------------------------------------------------------
  async function call(method, path, body) {
    const response = await fetch(API + path, {
      method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (!response.ok) {
      throw new Error((data && data.error) || ('HTTP ' + response.status));
    }
    return data;
  }

  function persist() {
    const snapshot = annotations.map((a) => ({ ...a }));
    saving = saving.then(async () => {
      try {
        const data = await call('PUT', '/annotations', snapshot);
        annotations = data.annotations;
        message = '';
        await refreshEntry();
      } catch (err) {
        message = 'Not saved: ' + err.message;
      }
      render();
    });
    return saving;
  }

  async function refreshEntry() {
    try {
      entry = await call('GET', '');
    } catch (err) {
      message = 'Daemon unreachable: ' + err.message;
      return;
    }
    if (loadedMtime === null) loadedMtime = entry.mtime;
    else if (entry.mtime && entry.mtime !== loadedMtime) changed = true;
  }

  // -- what changed since the last send ------------------------------------
  async function loadChanges() {
    const D = window.__annotateDiff;
    const C = window.__annotateChanges;
    if (!entry || !entry.baseline_at || !D || !C) { host.dataset.changes = 'none'; return; }
    let data;
    try {
      data = await call('GET', '/baseline');
    } catch (err) {
      message = 'Cannot compare with your last send: ' + err.message;
      host.dataset.changes = 'error';
      return;
    }
    changeView = C.apply(D.compare(data.reference, data.current, document), {
      at: data.at, flash,
      notes: annotations.map((a) => ({ target: anchorOf(a), sent: Boolean(a.sent_at) })),
      changed: () => render(),
      markRead,
    });
    host.dataset.changes = changeView ? 'shown' : 'none';
    const aimed = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (changeView && aimed) { changeView.reveal(aimed); aimed.scrollIntoView(); }
  }

  async function markRead() {
    try {
      await call('POST', '/baseline');
      location.reload();
    } catch (err) {
      message = 'Not marked as read: ' + err.message;
      render();
    }
  }

  // -- editor bubble -----------------------------------------------------------
  function openEditor(item, isNew, x, y) {
    closeEditor();
    editing = { item, isNew };
    const sent = Boolean(item.sent_at);
    const box = make('div', 'bubble');
    box.style.left = Math.max(8, Math.min(x, window.innerWidth + window.scrollX - 330)) + 'px';
    box.style.top = (y + 14) + 'px';
    const head = make('div', 'bubble-head', '#' + item.number + (sent ? ' · sent' : ''));
    const quote = make('div', 'bubble-quote', item.at ? item.at : (item.quote ? '“' + item.quote + '”' : ''));
    const area = make('textarea', 'bubble-text');
    area.value = item.note || '';
    area.readOnly = sent;
    area.placeholder = 'Your note (Enter saves, Shift+Enter new line, Esc cancels)';
    const actions = make('div', 'bubble-actions');
    const save = make('button', 'primary', sent ? 'Close' : 'Save');
    const cancel = make('button', '', 'Cancel');
    actions.append(save, cancel);
    if (!isNew) {
      const remove = make('button', 'danger', 'Delete');
      remove.addEventListener('click', () => {
        annotations = annotations.filter((a) => a.id !== item.id);
        closeEditor();
        persist();
      });
      actions.append(remove);
    }
    const commit = () => {
      const text = area.value.trim();
      if (!sent && text) {
        item.note = text;
        if (isNew) annotations.push(item);
        closeEditor();
        persist();
      } else {
        closeEditor();
      }
    };
    save.addEventListener('click', commit);
    cancel.addEventListener('click', closeEditor);
    area.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commit(); }
      if (e.key === 'Escape') { e.preventDefault(); closeEditor(); }
    });
    box.append(head, quote, area, actions);
    layer.append(box);
    // The layer's origin is not the page origin when <body> is positioned.
    const origin = layer.getBoundingClientRect();
    box.style.left = (parseFloat(box.style.left) - window.scrollX - origin.left) + 'px';
    box.style.top = (parseFloat(box.style.top) - window.scrollY - origin.top) + 'px';
    area.focus();
  }

  function closeEditor() {
    editing = null;
    layer.querySelectorAll('.bubble').forEach((b) => b.remove());
    render();
  }

  // -- rendering ------------------------------------------------------------
  // Pins are updated IN PLACE, keyed by annotation id. Recreating them on
  // every resize or poll removed the element under the cursor: a hovered
  // pin vanished without `mouseleave`, and its highlight stuck on the text.
  const pins = new Map();
  let flashed = null;

  function flash(node) {
    if (flashed) flashed.classList.remove('annotate-flash');
    flashed = node;
    if (node) node.classList.add('annotate-flash');
  }

  function pinFor(item) {
    let pin = pins.get(item.id);
    if (!pin) {
      pin = make('button', 'pin');
      pin.dataset.id = item.id;
      pin.addEventListener('click', (e) => {
        e.stopPropagation();
        const current = annotations.find((a) => a.id === pin.dataset.id);
        if (current) openEditor(current, false, e.pageX, e.pageY);
      });
      pin.addEventListener('mouseenter', () => flash(pin.target || null));
      pin.addEventListener('mouseleave', () => flash(null));
      pins.set(item.id, pin);
      layer.append(pin);
    }
    return pin;
  }

  function render() {
    const origin = layer.getBoundingClientRect();
    const orphans = [];
    const placed = new Set();
    let past = 0;
    for (const item of annotations) {
      const target = anchorOf(item);
      if (!target) {
        if (!item.sent_at) orphans.push(item);
        continue;
      }
      if (item.sent_at) past += 1;
      const box = visibleBox(target);
      const pin = pinFor(item);
      pin.className = 'pin' + (item.sent_at ? ' sent' : '');
      pin.textContent = (item.sent_at ? '✓' : '') + String(item.number);
      let wanted = box && (showPast || !item.sent_at) && !(changeView && changeView.hidden(target));
      if (box) {
        const dx = Math.min(Math.max(item.offset_x || 0, 0), Math.max(box.width, 0));
        const dy = Math.min(Math.max(item.offset_y || 0, 0), Math.max(box.height, 0));
        pin.style.left = (box.left - origin.left + dx) + 'px';
        pin.style.top = (box.top - origin.top + dy) + 'px';
        if (wanted && (scrolledOut(target, box.top + dy) || behindZoom(target, box.left + dx, box.top + dy))) {
          wanted = false;
        }
      }
      pin.style.display = wanted ? '' : 'none';
      pin.title = item.note;
      pin.target = target;
      placed.add(item.id);
    }
    for (const [id, pin] of pins) {
      if (!placed.has(id)) {
        if (flashed === pin.target) flash(null);
        pin.remove();
        pins.delete(id);
      }
    }
    // The bar is rebuilt at each render, every few seconds: its scroll stays.
    const kept = bar.scrollTop;
    renderBar(orphans, past);
    bar.scrollTop = kept;
  }

  function renderBar(orphans, past) {
    bar.textContent = '';
    bar.classList.toggle('collapsed', collapsed);
    const pending = annotations.filter((a) => !a.sent_at).length;
    const sent = annotations.length - pending;
    const top = make('div', 'bar-top');
    const counts = make('span', 'counts', pending + ' to send · ' + sent + ' sent' +
      (changeView ? ' · ' + changeView.total + ' changes' : ''));
    const toggle = make('button', 'toggle', collapsed ? '▴' : '▾');
    toggle.title = collapsed ? 'Expand' : 'Collapse';
    toggle.addEventListener('click', () => { collapsed = !collapsed; render(); });
    top.append(counts, toggle);
    bar.append(top);
    if (collapsed) return;
    if (changeView) bar.append(changeView.panel());

    const busy = entry && entry.status === 'delivered';
    const status = make('div', 'status' + (busy ? ' busy' : ''),
      'Status: ' + (entry ? (STATUS_TEXT[entry.status] || entry.status) : '…'));
    bar.append(status);
    if (entry) {
      bar.append(make('div', 'listening' + (entry.listening ? ' on' : ''),
        entry.listening
          ? 'An open session listens: Send delivers the notes to it.'
          : 'No open session listens: Send resumes its session in a terminal tab.'));
    }

    const actions = make('div', 'bar-actions');
    const send = make('button', 'primary', 'Send to session');
    send.addEventListener('click', () => trigger('send'));
    const fresh = make('button', '', 'New session');
    fresh.title = 'Open the notes in a NEW Claude Code session, in a terminal tab';
    fresh.addEventListener('click', () => trigger('new-session'));
    actions.append(send, fresh);
    // Notes sent, none waiting: the batch may not have reached the session
    // (an inbox can take a message Claude Code then ignores, inbox.py).
    if (entry && entry.sent_at && !annotations.some((a) => !a.sent_at)) {
      const again = make('button', '', 'Resend');
      again.title = 'Send the last batch of notes again, if the session never got it';
      again.addEventListener('click', () => trigger('resend'));
      actions.append(again);
    }
    if (changed) {
      const reload = make('button', 'primary', 'Reload');
      reload.title = 'The document changed on disk since this page was loaded';
      reload.addEventListener('click', () => location.reload());
      actions.append(reload);
    }
    bar.append(actions);
    if (changed) bar.append(make('div', 'message', 'The document changed on disk: reload to see it.'));

    if (orphans.length) {
      const list = make('div', 'orphans');
      list.append(make('div', 'orphans-title',
        orphans.length + ' note(s) lost their anchor (the passage changed):'));
      for (const item of orphans) {
        const row = make('div', 'orphan' + (item.sent_at ? ' sent' : ''));
        row.dataset.id = item.id;
        row.append(make('span', 'orphan-num', '#' + item.number + ' '),
          make('span', 'orphan-quote', item.quote ? '“' + item.quote.slice(0, 80) + '” ' : ''),
          make('span', 'orphan-note', item.note));
        row.addEventListener('click', (e) => openEditor(item, false, e.pageX - 340, e.pageY - 200));
        list.append(row);
      }
      bar.append(list);
    }
    if (message) bar.append(make('div', 'message', message));
    if (past) {
      const toggle = make('button', 'past-toggle',
        (showPast ? 'Hide' : 'Show') + ' past notes (' + past + ')');
      toggle.addEventListener('click', () => {
        showPast = !showPast;
        try { localStorage.setItem(PAST_KEY, showPast ? '1' : '0'); } catch (err) { /* this page only */ }
        render();
      });
      bar.append(toggle);
    }
    bar.append(make('div', 'hint', 'Alt+click on the document to annotate'));
  }

  async function trigger(action) {
    await saving;
    try {
      const result = await call('POST', '/' + action);
      message = result.message;
      const data = await call('GET', '/annotations');
      annotations = data.annotations;
      await refreshEntry();
    } catch (err) {
      message = err.message;
    }
    render();
  }

  // -- events ---------------------------------------------------------------
  document.addEventListener('mousedown', (e) => {
    if (!e.altKey || e.button !== 0 || isUi(e)) return;
    if (e.target.closest && e.target.closest('annotate-fold')) return;
    e.preventDefault();
    e.stopPropagation();
    suppressClick = true;
    const selection = squash(window.getSelection());
    const anchor = anchorFor(e.target);
    const rect = anchor.getBoundingClientRect();
    const id = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 16)
      : Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    const number = annotations.reduce((m, a) => Math.max(m, a.number || 0), 0) + 1;
    const item = {
      id, number, selector: selectorFor(anchor),
      quote: selection ? selection.slice(0, SELECTION_MAX) : squash(anchor.innerText || anchor.textContent).slice(0, QUOTE_MAX),
      at: selection ? '' : textAt(e),
      offset_x: Math.round(e.clientX - rect.left),
      offset_y: Math.round(e.clientY - rect.top),
      note: '', created_at: new Date().toISOString(), sent_at: '',
    };
    openEditor(item, true, e.pageX, e.pageY);
  }, true);

  document.addEventListener('click', (e) => {
    if (suppressClick || (e.altKey && !isUi(e))) {
      e.preventDefault();
      e.stopPropagation();
      suppressClick = false;
    }
  }, true);

  // The <base> injected for relative files would make `href="#x"` leave the
  // page: route fragment-only links to the current document instead.
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || isUi(e)) return;
    const link = e.target.closest && e.target.closest('a[href^="#"]');
    if (!link) return;
    e.preventDefault();
    const aimed = document.getElementById(decodeURIComponent(link.getAttribute('href').slice(1)));
    if (changeView && aimed) { changeView.reveal(aimed); render(); }
    location.hash = link.getAttribute('href');
  });

  let frame = 0;
  const schedule = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; if (!editing) render(); });
  };
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
  if (window.ResizeObserver) new ResizeObserver(schedule).observe(document.body);
  // A zoom or a <details> that opens: the pins inside it have a place now, and
  // they follow what scrolls inside it (the page's own scroll moves them anyway).
  new MutationObserver(() => {
    schedule();
    // A zoom opens with an animation of its box: the place is right once it ends.
    Promise.allSettled(document.getAnimations()
      .filter((a) => a.effect && a.effect.getTiming().iterations !== Infinity)
      .map((a) => a.finished)).then(schedule);
  }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['open'] });
  document.addEventListener('scroll', (e) => { if (e.target !== document) schedule(); }, true);

  async function poll() {
    await refreshEntry();
    if (!editing) render();
    setTimeout(poll, 4000);
  }

  (async () => {
    try {
      const data = await call('GET', '/annotations');
      annotations = data.annotations;
    } catch (err) {
      message = 'Cannot load the notes: ' + err.message;
    }
    await refreshEntry();
    await loadChanges();
    render();
    setTimeout(poll, 4000);
  })();
})();
