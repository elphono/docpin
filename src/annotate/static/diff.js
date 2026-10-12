/*
 * annotate diff: what changed between the reference (the document as it was
 * at the last "Send to session") and the file on disk now.
 *
 * Pure comparison: changes.js draws the result. Both versions are parsed by
 * DOMParser (no script runs, nothing loads), cut into blocks, and the blocks
 * matched by their text with Myers' diff, the one git uses for lines. Every
 * text belongs to ONE block, the nearest: a block's signature is its own text,
 * without the text of the blocks it contains (a parent <li> whose own words
 * change is seen, a wrapper <div> with no text of its own is ignored).
 *
 * Comparing source with source keeps what the document's scripts add to the
 * page out of the comparison. Each changed block is then found again in the
 * live page by its path, and kept only if its signature there is the same:
 * a block that cannot be found counts in `lost`, never a mark on a neighbour.
 */
(() => {
  'use strict';
  const UNIT = 'p,li,tr,h1,h2,h3,h4,h5,h6,dt,dd,blockquote,pre,figcaption,caption,' +
    'summary,div,svg,img';
  const SKIP = new Set(['script', 'style', 'template', 'noscript']);
  const MAX_EDITS = 2000;
  const PAIR_MIN = 0.5;
  const WORD = /[\p{L}\p{N}]+/gu;

  const squash = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const wordsOf = (text) => (String(text).toLowerCase().match(WORD) || []);
  const isUnit = (node) => node.nodeType === 1 && node.matches(UNIT);

  // The text nodes that belong to `node` itself: not inside a nested block,
  // a script or a style. An <svg> has none: it is compared by its markup.
  function ownTextNodes(node) {
    const out = [];
    if (node.localName === 'svg') return out;
    const walk = (parent) => {
      for (const child of parent.childNodes) {
        if (child.nodeType === 3) out.push(child);
        else if (child.nodeType === 1 && !SKIP.has(child.localName) && !isUnit(child)) walk(child);
      }
    };
    walk(node);
    return out;
  }

  const ownText = (node) => squash(ownTextNodes(node).map((t) => t.data).join(''));

  function signature(node) {
    if (node.localName === 'svg') return 'svg:' + squash(node.outerHTML);
    if (node.localName === 'img') {
      return 'img:' + (node.getAttribute('src') || '') + '|' + (node.getAttribute('alt') || '');
    }
    const text = ownText(node);
    return text ? node.localName + ':' + text : '';
  }

  function shown(node) {
    if (node.localName === 'svg') return squash(node.textContent) || 'a figure';
    if (node.localName === 'img') return node.getAttribute('alt') || node.getAttribute('src') || 'an image';
    return ownText(node);
  }

  // The same path as the overlay's selectorFor, computed in `doc`.
  function pathOf(node, doc) {
    const parts = [];
    while (node && node.nodeType === 1 && node !== doc.body && node !== doc.documentElement) {
      if (node.id && doc.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
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

  function units(doc) {
    const out = [];
    if (!doc.body) return out;
    for (const node of doc.body.querySelectorAll(UNIT)) {
      if (node.parentElement && node.parentElement.closest('svg')) continue;
      const sig = signature(node);
      if (!sig) continue;
      const text = shown(node);
      out.push({ node, sig, tag: node.localName, text, words: wordsOf(text), live: null });
    }
    return out;
  }

  // Myers' O(ND) diff. Common head and tail are trimmed first; the trace keeps
  // only the live diagonals of each step, so memory grows as D², not D·(N+M).
  function myers(a, b, max) {
    let head = 0;
    while (head < a.length && head < b.length && a[head] === b[head]) head++;
    let ta = a.length;
    let tb = b.length;
    while (ta > head && tb > head && a[ta - 1] === b[tb - 1]) { ta--; tb--; }
    const A = a.slice(head, ta);
    const B = b.slice(head, tb);
    const N = A.length;
    const M = B.length;
    const off = N + M + 1;
    const v = new Int32Array(2 * off + 1);
    const trace = [];
    let D = 0;
    search:
    for (let d = 0; d <= N + M; d++) {
      if (d > max) return null;
      trace.push(v.slice(off - d, off + d + 1));
      for (let k = -d; k <= d; k += 2) {
        let x = (k === -d || (k !== d && v[off + k - 1] < v[off + k + 1]))
          ? v[off + k + 1] : v[off + k - 1] + 1;
        let y = x - k;
        while (x < N && y < M && A[x] === B[y]) { x++; y++; }
        v[off + k] = x;
        if (x >= N && y >= M) { D = d; break search; }
      }
    }
    const ops = [];
    let x = N;
    let y = M;
    for (let d = D; d > 0; d--) {
      const prev = trace[d];                 // diagonal k is at index k + d
      const k = x - y;
      const down = k === -d || (k !== d && prev[k - 1 + d] < prev[k + 1 + d]);
      const pk = down ? k + 1 : k - 1;
      const px = prev[pk + d];
      const py = px - pk;
      while (x > px && y > py) { ops.push(['=', head + x - 1, head + y - 1]); x--; y--; }
      if (down) ops.push(['+', -1, head + y - 1]);
      else ops.push(['-', head + x - 1, -1]);
      x = px;
      y = py;
    }
    while (x > 0 && y > 0) { ops.push(['=', head + x - 1, head + y - 1]); x--; y--; }
    ops.reverse();
    const out = [];
    for (let i = 0; i < head; i++) out.push(['=', i, i]);
    for (const op of ops) out.push(op);
    for (let i = 0; i < a.length - ta; i++) out.push(['=', ta + i, tb + i]);
    return out;
  }

  function jaccard(a, b) {
    const sa = new Set(a);
    const sb = new Set(b);
    if (!sa.size && !sb.size) return 1;
    let common = 0;
    for (const w of sa) if (sb.has(w)) common++;
    return common / (sa.size + sb.size - common);
  }

  const similar = (old, unit) => old.tag === unit.tag && jaccard(old.words, unit.words) >= PAIR_MIN;

  function changed(old, unit) {
    const ops = myers(old.words, unit.words, Infinity) || [];
    const added = ops.filter((op) => op[0] === '+').map((op) => op[2]);
    return { kind: 'changed', unit, oldText: old.text, added };
  }

  function locate(list, doc, live) {
    for (const unit of list) {
      let node = null;
      try { node = live.querySelector(pathOf(unit.node, doc)); } catch (err) { node = null; }
      unit.live = node && signature(node) === unit.sig ? node : null;
    }
  }

  function compare(referenceHtml, currentHtml, live) {
    const parser = new DOMParser();
    const ref = units(parser.parseFromString(referenceHtml, 'text/html'));
    const doc = parser.parseFromString(currentHtml, 'text/html');
    const cur = units(doc);
    const ops = myers(ref.map((u) => u.sig), cur.map((u) => u.sig), MAX_EDITS);
    if (!ops) return { tooMany: true, units: cur, changes: [], lost: 0 };
    const changes = [];
    let removed = [];
    let added = [];
    let lastSame = -1;
    const flush = (nextSame) => {
      const paired = new Map();
      let from = 0;
      for (const r of removed) {
        for (let j = from; j < added.length; j++) {
          if (similar(ref[r], cur[added[j]])) { paired.set(added[j], r); from = j + 1; break; }
        }
      }
      const kept = new Set(paired.values());
      for (const j of added) {
        changes.push(paired.has(j) ? changed(ref[paired.get(j)], cur[j]) : { kind: 'new', unit: cur[j] });
      }
      const at = added.length ? added[added.length - 1] : lastSame;
      for (const r of removed) {
        if (kept.has(r)) continue;
        const anchor = at >= 0 ? cur[at] : (nextSame >= 0 ? cur[nextSame] : null);
        changes.push({ kind: 'removed', text: ref[r].text, anchor,
          place: at >= 0 ? 'after' : 'before' });
      }
      removed = [];
      added = [];
    };
    for (const [op, i, j] of ops) {
      if (op === '=') { flush(j); lastSame = j; }
      else if (op === '-') removed.push(i);
      else added.push(j);
    }
    flush(-1);
    locate(cur, doc, live);
    const lost = changes.filter((c) => !(c.kind === 'removed' ? c.anchor && c.anchor.live : c.unit.live)).length;
    return { tooMany: false, units: cur, changes, lost };
  }

  window.__annotateDiff = { compare, myers, ownTextNodes, wordsOf, MAX_EDITS };
})();
