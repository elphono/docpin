/*
 * annotate changes: draws in the live page what diff.js found.
 *
 * The page receives classes (annotate-new, annotate-changed, annotate-folded)
 * and one element of its own, <annotate-fold>. Pins are anchored by nth-of-type
 * paths (selectorFor), which count siblings of the SAME tag name: neither a
 * class nor <annotate-fold> moves an anchor.
 *
 * A mark is a soft glow around the block, one colour per kind, drawn by the
 * stylesheet: no label over the page, nothing that can cover a text. A removed
 * passage is not in the page at all: it waits, closed, in the panel. So does
 * the text a changed block had before.
 *
 * Nothing is drawn when nothing changed (the witness), and nothing folds when
 * a change could not be found in the page: folding could hide it.
 *
 * One mark per new PART, not per new block (placeMarks). A block inside a new
 * block gets no mark of its own. A new block climbs to the highest element
 * that holds nothing but new blocks (a new list, table, card), which carries
 * the one mark. A group is what a reader takes in as one thing: a container
 * splits into its direct children when at least two of them are or contain a
 * heading h1-h6 (a grid of cards, a page), and each child is then a group.
 * Under a CHANGED block, a new or changed block keeps its own mark.
 *
 * A change inside a closed <dialog> (a zoom of an ELI document) has no box: the
 * element that opens that dialog glows for it, and the navigator opens it.
 */
(() => {
  'use strict';
  const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'];
  const ANY_HEADING = HEADINGS.join(',');
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

  // The items to draw, in document order: the changed ones not inside a new
  // part, one item per new part (an unlocated change is not here: it is
  // counted, never drawn).
  function placeMarks(result, located) {
    const fresh = new Set(located.filter((i) => i.kind === 'new').map((i) => i.target));
    // One pass: per element, the blocks it holds and how many of them are new.
    const held = new Map();
    for (const u of result.units) {
      if (!u.live) continue;
      const isNew = fresh.has(u.live);
      for (let up = u.live; up && up !== document.body; up = up.parentElement) {
        const c = held.get(up) || { blocks: 0, fresh: 0, one: u.live };
        c.blocks += 1;
        if (isNew) c.fresh += 1;
        held.set(up, c);
      }
    }
    const allNew = (el) => { const c = held.get(el); return Boolean(c) && c.blocks === c.fresh; };
    const blocks = new Set(result.units.filter((u) => u.live).map((u) => u.live));
    const titled = (el) => HEADINGS.includes(el.localName) || Boolean(el.querySelector(ANY_HEADING));
    const split = (el) => {
      if (blocks.has(el)) return [el];                    // a block with text of its own
      const kids = [...el.children].filter((c) => held.has(c));
      if (kids.filter(titled).length >= 2) return kids.flatMap(split);
      const c = held.get(el);
      return [c.blocks === 1 ? c.one : el];               // a lone block, not its wrapper
    };
    const tops = new Set();
    for (const node of fresh) {
      let top = node;
      for (let up = node.parentElement; up && up !== document.body && allNew(up); up = up.parentElement) top = up;
      tops.add(top);
    }
    const groups = new Set([...tops].flatMap(split));
    const under = (node) => {
      for (let up = node.parentElement; up; up = up.parentElement) {
        if (groups.has(up) || fresh.has(up)) return true;
      }
      return false;
    };
    const groupOf = (node) => {
      for (let n = node; n; n = n.parentElement) if (groups.has(n)) return n;
      return null;
    };
    const drawn = [];
    const shown = new Set();
    for (const item of located) {
      if (item.kind === 'changed') { if (!under(item.target)) drawn.push(item); continue; }
      const group = groupOf(item.target);
      if (!group) { drawn.push(item); continue; }
      if (shown.has(group) || under(group)) continue;
      shown.add(group);
      drawn.push(group === item.target ? item : { kind: 'new', change: null, target: group,
        text: group.innerText.replace(/\s+/g, ' ').trim(), part: true });
    }
    return drawn;
  }

  // What opens a closed <dialog>, by the convention of the ELI documents.
  const openerOf = (dialog) => (dialog.id
    ? document.querySelector('[data-zoom="' + CSS.escape(dialog.id) + '"]') : null);
  // The element a reader can see for this node: itself, or what opens its dialog.
  const seenAs = (node) => {
    const dialog = node.closest('dialog');
    return (dialog && !dialog.open && openerOf(dialog)) || node;
  };

  function apply(result, opts) {
    const { flash, notes } = opts;
    const items = result.changes.map((change) => ({
      kind: change.kind, change,
      target: change.kind === 'removed' ? (change.anchor && change.anchor.live) : change.unit.live,
      text: change.kind === 'removed' ? change.text : change.unit.text,
    }));
    if (!result.tooMany && !items.length) return null;
    const located = items.filter((i) => i.target);
    const gone = items.filter((i) => i.kind === 'removed');
    const drawn = placeMarks(result, located.filter((i) => i.kind !== 'removed'));
    const drawnSet = new Set(drawn);
    const parts = drawn.filter((i) => i.part);           // a new part that is no block itself
    const absorbed = new Set(located.filter((i) => i.kind !== 'removed' && !drawnSet.has(i)));
    const counted = items.filter((i) => !absorbed.has(i)).concat(parts);
    for (const item of drawn) {
      item.target.classList.add(item.kind === 'new' ? 'annotate-new' : 'annotate-changed');
    }
    // Whatever changed inside a closed dialog shows on what opens it.
    for (const item of located) {
      const seen = seenAs(item.target);
      if (seen !== item.target && !seen.matches('.annotate-new,.annotate-changed')) {
        seen.classList.add('annotate-changed');
      }
    }
    highlight(drawn);

    let sections = [];
    const level = foldLevel();
    if (level) sections = sectionsOf(level);
    const sectionOf = (node) => sections.find((s) => inside(s, node)) || null;
    for (const item of located.concat(parts)) {
      item.section = sectionOf(item.target) || sectionOf(seenAs(item.target));
    }

    const unfold = (sec) => {
      if (!sec.fold) return;
      sec.members.forEach((m) => m.classList.remove('annotate-folded'));
      sec.fold.remove();
      sec.fold = null;
    };
    const fold = (sec) => {
      const blocks = result.units.filter((u) => u.live && sec.members.some((m) => m === u.live || m.contains(u.live))).length;
      if (!blocks) return;                               // nothing to hide: no bar saying so
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
      }
    }

    let index = -1;
    const reveal = (node) => {
      for (const sec of sections) if (sec.fold && inside(sec, node)) unfold(sec);
    };
    // Everything between the reader and this node: a folded section, a closed
    // <details> or zoom, another zoom open on top of the page.
    const bringOut = (node) => {
      reveal(node);
      document.querySelectorAll('dialog[open]').forEach((d) => { if (!d.contains(node)) d.close(); });
      for (let up = node.parentElement; up; up = up.parentElement) {
        if (up.localName === 'details') up.open = true;
      }
      const dialog = node.closest('dialog');
      if (dialog && !dialog.open) {
        const opener = openerOf(dialog);
        if (opener) { reveal(opener); opener.click(); }
        if (!dialog.open) dialog.show();
      }
    };
    const visit = (node) => {
      bringOut(node);
      opts.changed();
      node.scrollIntoView({ block: 'center' });
      flash(node);
      setTimeout(() => flash(null), 1200);
    };
    const go = (i) => {
      index = i;
      follow = true;
      visit(drawn[i].target);
    };
    let follow = false;          // the panel brings the current row into its list
    let listTop = 0;             // the panel is rebuilt at each render: its scroll is kept here
    let goneOpen = false;
    const view = {
      items: drawn,
      total: counted.length,
      hidden: (node) => Boolean(node && node.closest && node.closest('.annotate-folded')),
      reveal,
      showAll() { sections.forEach(unfold); },
      next() { if (drawn.length) go((index + 1) % drawn.length); },
      prev() { if (drawn.length) go((index - 1 + drawn.length) % drawn.length); },
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
          cell.append(make('b', '', String(counted.filter((i) => i.kind === kind).length)), label);
          counts.append(cell);
        }
        box.append(counts);
        if (result.lost) {
          box.append(make('div', 'chg-lost', result.lost + ' change' + (result.lost === 1 ? '' : 's') +
            ' not located: the page differs from the file (a script?); nothing is folded.'));
        }
        if (drawn.length) {
          const navBar = make('div', 'chg-nav');
          const up = make('button', '', '▲');
          const down = make('button', '', '▼');
          up.addEventListener('click', () => view.prev());
          down.addEventListener('click', () => view.next());
          navBar.append(up, make('span', '', (index < 0 ? '–' : index + 1) + ' / ' + drawn.length), down);
          box.append(navBar);
          const list = make('ul', 'chg-list');
          let current = null;
          drawn.forEach((item, i) => {
            const row = make('li', i === index ? 'cur' : '');
            const where = item.section ? cut(item.section.heading.textContent.trim(), 18) + ' · ' : '';
            // A new block that holds absorbed ones reads by all its text, not its
            // own (a card's own text is its number).
            const holds = item.kind === 'new' && !item.part && [...absorbed].some((a) => item.target.contains(a.target));
            const text = holds ? item.target.innerText.replace(/\s+/g, ' ').trim() : item.text;
            row.append(make('i', 'dot ' + item.kind), make('span', '', where + cut(text, 44)));
            row.addEventListener('click', () => go(i));
            list.append(row);
            if (i !== index) return;
            current = row;
            if (item.kind === 'changed' && item.change.oldText) {
              list.append(make('li', 'chg-before', 'Before: ' + item.change.oldText));
            }
          });
          list.addEventListener('scroll', () => { listTop = list.scrollTop; });
          box.append(list);
          // Once the panel is in the bar: an element out of it has no scroll.
          queueMicrotask(() => {
            list.scrollTop = listTop;
            if (follow && current) {
              const top = current.offsetTop - list.offsetTop;
              if (top < list.scrollTop || top + current.offsetHeight > list.scrollTop + list.clientHeight) {
                list.scrollTop = top;
              }
            }
            follow = false;
          });
        }
        if (gone.length) {
          const toggle = make('button', 'chg-gone-toggle', (goneOpen ? '▾ ' : '▸ ') + gone.length +
            ' removed passage' + (gone.length === 1 ? '' : 's'));
          toggle.addEventListener('click', () => { goneOpen = !goneOpen; opts.changed(); });
          box.append(toggle);
          if (goneOpen) {
            const list = make('ul', 'chg-list chg-gone');
            for (const item of gone) {
              const row = make('li', '', item.text);
              if (item.target) row.addEventListener('click', () => visit(item.target));
              list.append(row);
            }
            box.append(list);
          }
        }
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
