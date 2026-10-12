/*
 * annotate changes: draws in the live page what diff.js found.
 *
 * The page receives classes (annotate-new, annotate-changed, annotate-dim,
 * annotate-folded) and one element of its own, <annotate-fold>. Pins are
 * anchored by nth-of-type paths (selectorFor), which count siblings of the
 * SAME tag name: neither a class nor <annotate-fold> moves an anchor. Tags,
 * removed markers and old text live in the overlay layer, like the pins.
 *
 * Nothing is drawn when nothing changed (the witness), and nothing folds when
 * a change could not be found in the page: folding could hide it.
 */
(() => {
  'use strict';
  const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'];
  const LIST_MAX = 8;
  const IGNORED = new Set(['annotate-root', 'script', 'style', 'link', 'annotate-fold']);

  const make = (name, cls, text) => {
    const node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

  // The highest heading level that comes back at least twice.
  function foldLevel() {
    for (const tag of HEADINGS) {
      if (document.body.querySelectorAll(tag).length >= 2) return tag;
    }
    return null;
  }

  function sectionsOf(tag) {
    const stops = HEADINGS.slice(0, Number(tag[1])).join(',');
    return [...document.body.querySelectorAll(tag)].map((heading) => {
      const members = [];
      for (let s = heading.nextElementSibling; s; s = s.nextElementSibling) {
        if (s.matches(stops) || s.querySelector(stops)) break;
        if (!IGNORED.has(s.localName)) members.push(s);
      }
      return { heading, members, fold: null };
    });
  }

  const inside = (sec, node) => Boolean(node) &&
    (node === sec.heading || sec.members.some((m) => m === node || m.contains(node)));

  function highlight(items) {
    if (!(window.CSS && CSS.highlights && window.Highlight)) return;
    const D = window.__annotateDiff;
    const ranges = [];
    for (const item of items) {
      if (item.kind !== 'changed' || !item.target) continue;
      const found = [];
      for (const text of D.ownTextNodes(item.target)) {
        const re = /[\p{L}\p{N}]+/gu;
        for (let m = re.exec(text.data); m; m = re.exec(text.data)) {
          found.push([text, m.index, m.index + m[0].length]);
        }
      }
      if (found.length !== item.change.unit.words.length) continue;   // split words: block mark only
      for (const index of item.change.added) {
        const [text, start, end] = found[index];
        const range = document.createRange();
        range.setStart(text, start);
        range.setEnd(text, end);
        ranges.push(range);
      }
    }
    CSS.highlights.set('annotate-added', new Highlight(...ranges));
  }

  function apply(result, opts) {
    const { layer, flash, notes } = opts;
    const items = result.changes.map((change) => ({
      kind: change.kind, change,
      target: change.kind === 'removed' ? (change.anchor && change.anchor.live) : change.unit.live,
      text: change.kind === 'removed' ? change.text : change.unit.text,
    }));
    if (!result.tooMany && !items.length) return null;
    const located = items.filter((i) => i.target);
    const marked = new Set();
    for (const item of located) {
      if (item.kind === 'removed') continue;
      item.target.classList.add(item.kind === 'new' ? 'annotate-new' : 'annotate-changed');
      marked.add(item.target);
    }
    highlight(located);

    let sections = [];
    const level = foldLevel();
    if (level) sections = sectionsOf(level);
    const sectionOf = (node) => sections.find((s) => inside(s, node)) || null;
    for (const item of located) item.section = sectionOf(item.target);

    const unfold = (sec) => {
      if (!sec.fold) return;
      sec.members.forEach((m) => m.classList.remove('annotate-folded'));
      sec.fold.remove();
      sec.fold = null;
    };
    const fold = (sec) => {
      const blocks = result.units.filter((u) => u.live && sec.members.some((m) => m === u.live || m.contains(u.live))).length;
      const treated = notes.filter((n) => n.sent && inside(sec, n.target)).length;
      const bar = document.createElement('annotate-fold');
      bar.append(make('b', '', '▸ Already read · unchanged'),
        make('span', '', blocks + ' block' + (blocks === 1 ? '' : 's') +
          (treated ? ' · ' + treated + ' treated note' + (treated === 1 ? '' : 's') : '')));
      const show = make('button', '', 'Show');
      show.type = 'button';
      show.addEventListener('click', () => { unfold(sec); opts.changed(); });
      bar.append(show);
      sec.members.forEach((m) => m.classList.add('annotate-folded'));
      sec.heading.after(bar);
      sec.fold = bar;
    };
    if (!result.tooMany && result.lost === 0) {
      for (const sec of sections) {
        const changedHere = located.some((i) => i.section === sec);
        const unsent = notes.some((n) => !n.sent && inside(sec, n.target));
        if (!changedHere && !unsent) fold(sec);
        if (changedHere) {
          for (const u of result.units) {
            if (!u.live || marked.has(u.live) || !inside(sec, u.live) || u.live === sec.heading) continue;
            if ([...marked].some((m) => u.live.contains(m))) continue;
            u.live.classList.add('annotate-dim');
          }
        }
      }
    }

    // Overlay layer: a tag per marked block, a marker per removed passage.
    const tags = [];
    let open = null;
    const oldBox = make('div', 'chg-old');
    oldBox.hidden = true;
    layer.append(oldBox);
    const showOld = (item, anchor) => {
      oldBox.textContent = item.kind === 'removed' ? item.text : item.change.oldText;
      oldBox.hidden = false;
      open = { item, anchor };
      view.layout(layer.getBoundingClientRect());
    };
    const hideOld = () => { oldBox.hidden = true; open = null; };
    for (const item of located) {
      const el = item.kind === 'removed'
        ? make('button', 'chg-removed', '− removed · view')
        : make('span', 'chg-tag ' + item.kind, item.kind === 'new' ? 'NEW' : 'CHANGED');
      if (item.kind === 'removed') {
        el.addEventListener('click', () => (open && open.item === item ? hideOld() : showOld(item, el)));
      } else if (item.kind === 'changed') {
        el.title = 'Hover: the text before';
        el.addEventListener('mouseenter', () => showOld(item, el));
        el.addEventListener('mouseleave', hideOld);
      }
      layer.append(el);
      tags.push({ item, el });
    }

    let index = -1;
    const reveal = (node) => {
      for (const sec of sections) if (sec.fold && inside(sec, node)) unfold(sec);
    };
    const go = (i) => {
      index = i;
      const item = located[i];
      reveal(item.target);
      opts.changed();
      item.target.scrollIntoView({ block: 'center' });
      flash(item.target);
      setTimeout(() => flash(null), 1200);
    };
    const view = {
      items: located,
      total: items.length,
      hidden: (node) => Boolean(node && node.closest && node.closest('.annotate-folded')),
      reveal,
      showAll() {
        sections.forEach(unfold);
        document.querySelectorAll('.annotate-dim').forEach((n) => n.classList.remove('annotate-dim'));
      },
      next() { if (located.length) go((index + 1) % located.length); },
      prev() { if (located.length) go((index - 1 + located.length) % located.length); },
      layout(origin) {
        for (const { item, el } of tags) {
          const box = item.target.getBoundingClientRect();
          const visible = (box.width || box.height) && !view.hidden(item.target);
          el.style.display = visible ? '' : 'none';
          if (!visible) continue;
          if (item.kind === 'removed') {
            // On the left, where the passage was: the bar covers the right edge.
            const y = item.change.place === 'after' ? box.bottom + 2 : box.top - 20;
            el.style.left = (box.left - origin.left) + 'px';
            el.style.top = (y - origin.top) + 'px';
          } else {
            el.style.left = (box.left - origin.left + 6) + 'px';
            el.style.top = (box.top - origin.top - 17) + 'px';
          }
        }
        if (open) {
          const a = open.anchor.getBoundingClientRect();
          oldBox.style.left = (a.left - origin.left) + 'px';
          oldBox.style.top = (a.bottom - origin.top + 4) + 'px';
        }
      },
      panel() {
        const box = make('div', 'chg');
        box.append(make('div', 'chg-head', 'Changes since your last send'));
        if (opts.at) box.append(make('div', 'chg-when', 'sent ' + new Date(opts.at).toLocaleString()));
        if (result.tooMany) {
          box.append(make('div', 'chg-lost', 'Too many changes to compare: the document is shown as is.'));
          return box;
        }
        const counts = make('div', 'chg-counts');
        for (const [kind, label] of [['new', 'new'], ['changed', 'changed'], ['removed', 'removed']]) {
          const cell = make('div', kind);
          cell.append(make('b', '', String(items.filter((i) => i.kind === kind).length)), label);
          counts.append(cell);
        }
        box.append(counts);
        if (result.lost) {
          box.append(make('div', 'chg-lost', result.lost + ' change' + (result.lost === 1 ? '' : 's') +
            ' not located: the page differs from the file (a script?); nothing is folded.'));
        }
        const navBar = make('div', 'chg-nav');
        const up = make('button', '', '▲');
        const down = make('button', '', '▼');
        up.addEventListener('click', () => view.prev());
        down.addEventListener('click', () => view.next());
        navBar.append(up, make('span', '', (index < 0 ? '–' : index + 1) + ' / ' + located.length), down);
        box.append(navBar);
        const list = make('ul', 'chg-list');
        located.slice(0, LIST_MAX).forEach((item, i) => {
          const row = make('li', i === index ? 'cur' : '');
          const where = item.section ? cut(item.section.heading.textContent.trim(), 18) + ' · ' : '';
          row.append(make('i', 'dot ' + item.kind), make('span', '', where + cut(item.text, 44)));
          row.addEventListener('click', () => go(i));
          list.append(row);
        });
        if (located.length > LIST_MAX) list.append(make('li', 'more', '+' + (located.length - LIST_MAX) + ' more'));
        box.append(list);
        const actions = make('div', 'chg-actions');
        const all = make('button', '', 'Show everything');
        all.addEventListener('click', () => { view.showAll(); opts.changed(); });
        const read = make('button', 'primary', 'Mark as read');
        read.addEventListener('click', () => opts.markRead());
        actions.append(all, read);
        box.append(actions);
        return box;
      },
    };
    return view;
  }

  window.__annotateChanges = { apply };
})();
