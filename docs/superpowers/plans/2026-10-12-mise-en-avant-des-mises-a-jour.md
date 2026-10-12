# Mise en avant de ce qui a changé — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** quand une session met à jour un document déjà lu, la page met en avant ce qui a changé depuis le dernier « Send to session » et replie les sections inchangées.

**Architecture:** au moment de l'envoi (`Sender._settle`, seul endroit où des notes deviennent envoyées), le démon copie le fichier dans `baselines/<id>.html` sous son dossier de données. La page lit la copie et le fichier actuel par `GET /api/docs/<id>/baseline`, les compare dans le navigateur (`static/diff.js`, pur), puis les dessine (`static/changes.js`) ; `annotate.js` ne fait que brancher les deux et afficher le panneau dans sa barre.

**Tech Stack:** Python 3 bibliothèque standard (démon), JavaScript sans build (overlay), pytest, Playwright pour les tests navigateur.

**Spec:** `docs/2026-10-12-mise-en-avant-des-mises-a-jour.html` (doc docpin `dfc5f500`).

## Global Constraints

- Aucune dépendance d'exécution hors bibliothèque standard (`dependencies = []`).
- La copie de référence ne vit **jamais** à côté du document : `config.data_dir()/baselines/<id>.html`.
- Anglais dans le code : identifiants, commentaires, libellés (« Changes since your last send », « Already read · unchanged », « Show », « Show everything », « Mark as read », « NEW », « CHANGED », « removed »).
- Français dans les messages de commit ; aucune mention de Claude, d'IA ou de co-auteur.
- Toute requête qui modifie exige `X-Annotate` et une origine locale (déjà assuré par `_check_client`).
- La page ne reçoit que des classes `annotate-*` et des éléments `<annotate-fold>` : jamais de `div`/`p` inséré, jamais d'enveloppe (les ancres `nth-of-type` ne doivent pas bouger).
- Seuils : appariement « modifié » à Jaccard ≥ 0,5 sur les mots, même balise ; renoncement au-delà de 2 000 différences.
- Aucune marque, aucun repli quand la référence est identique au fichier (témoin).
- Aucun repli dès qu'un changement n'a pas été retrouvé dans la page.
- Un échec d'écriture de la copie n'annule pas l'envoi : il retire la référence et le dit dans `last_error`.

## Review Focus

1. Un document dont les scripts réécrivent du texte : le bloc changé n'est pas retrouvé → compté « not located », et rien ne se replie (Task 4, étape « clock »).
2. Un lien `#ancre` vers un bloc d'une section repliée : la section se déplie avant le saut (Task 4).
3. Un document réécrit au-delà de 2 000 différences : « Too many changes to compare », aucune marque (Task 3, contrôle pur).
4. Le texte propre d'un `li` parent qui change alors que sa sous-liste reste : marqué « changed » (Task 3, contrôle pur).
5. Un Alt+clic pendant le repli : la note s'ancre au bloc visé, et le pliage n'accepte pas de note sur lui-même (Task 4).

---

### Task 1: la copie de référence, prise à l'envoi

**Files:**
- Modify: `src/annotate/registry.py` (docstring de disposition, `baseline_path`, `take_baseline`, `read_baseline`, `forget`)
- Modify: `src/annotate/sender.py` (`_settle`)
- Create: `tests/test_baseline.py`

**Interfaces:**
- Produces: `registry.baseline_path(doc_id: str) -> Path` ; `registry.take_baseline(doc_id: str, stamp: str) -> str` (rend `""` si prise, sinon la raison ; écrit `baseline_at` = `stamp` ou `""` et, en cas d'échec, `last_error`) ; `registry.read_baseline(doc_id: str) -> str | None`.

- [ ] **Step 1: écrire les tests**

```python
"""The reference of "what changed": the document as it was at the last send.

A send copies the file under the data folder (never next to the document);
the page compares that copy with the file on disk. A refused send keeps the
previous copy; a copy that cannot be written REMOVES it, because comparing
with an older version would mark as new what the reader already read.
"""
from __future__ import annotations

import pytest

from annotate import config, registry, sender, terminal
from annotate.config import Config
from fakes import RecordingOpener


def note(id_, text):
    return {"id": id_, "selector": "body > p:nth-of-type(1)", "quote": "q", "note": text,
            "offset_x": 0, "offset_y": 0}


def page(text):
    return f"<!doctype html><html><head><title>T</title></head><body><p>{text}</p></body></html>"


@pytest.fixture
def doc(make_doc):
    path = make_doc("<p>version one</p>")
    return path, registry.register(path, session="sess-1")["id"]


def send_note(send, doc_id, id_):
    items = registry.list_annotations(doc_id)
    registry.replace_annotations(doc_id, items + [note(id_, "fix " + id_)])
    return send.dispatch(doc_id)


def test_a_send_keeps_the_document_as_it_was_as_the_reference(doc):
    path, doc_id = doc
    sent_text = path.read_text()
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    path.write_text(page("the session corrected this"))
    assert registry.read_baseline(doc_id) == sent_text
    entry = registry.get(doc_id)
    assert entry["baseline_at"] == entry["sent_at"] != ""


def test_three_sends_keep_one_reference_the_last(doc):
    path, doc_id = doc
    send = sender.Sender(Config(), opener=RecordingOpener())
    for round_ in range(3):
        path.write_text(page(f"version {round_}"))
        send_note(send, doc_id, f"n{round_}")
        assert registry.read_baseline(doc_id) == page(f"version {round_}")
    assert [p.name for p in registry.baseline_path(doc_id).parent.iterdir()] == [f"{doc_id}.html"]


def test_a_refused_send_keeps_the_previous_reference(doc):
    path, doc_id = doc
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    first = registry.read_baseline(doc_id)
    path.write_text(page("edited after the send"))
    failing = sender.Sender(Config(), opener=RecordingOpener(
        fail=terminal.TerminalError("wt.exe exited 1")))
    for _ in range(2):                                   # the next cycle too
        with pytest.raises(sender.SendError):
            send_note(failing, doc_id, "n2")
        assert registry.read_baseline(doc_id) == first
    with pytest.raises(sender.NothingToSend):
        sender.Sender(Config(), opener=RecordingOpener()).dispatch(doc_id) \
            if not registry.pending(registry.list_annotations(doc_id)) else (_ for _ in ()).throw(
                sender.NothingToSend("pending note left"))
    assert registry.read_baseline(doc_id) == first


def test_a_reference_that_cannot_be_written_is_removed_and_said(doc, monkeypatch):
    path, doc_id = doc
    send = sender.Sender(Config(), opener=RecordingOpener())
    send_note(send, doc_id, "n1")
    real = registry.atomic_write

    def full_disk(target, text):
        assert isinstance(text, str)                     # the real one takes a str
        if target.parent.name == "baselines":
            raise OSError(28, "No space left on device")
        return real(target, text)

    monkeypatch.setattr(registry, "atomic_write", full_disk)
    for round_ in (2, 3):                                # twice: no crash, still nothing
        path.write_text(page(f"version {round_}"))
        result = send_note(send, doc_id, f"n{round_}")
        assert result["count"] == 1                      # the notes went anyway
        assert registry.read_baseline(doc_id) is None
        entry = registry.get(doc_id)
        assert entry["baseline_at"] == ""
        assert "reference" in entry["last_error"] and "No space" in entry["last_error"]


def test_the_reference_never_lives_next_to_the_document(doc):
    path, doc_id = doc
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
    assert registry.baseline_path(doc_id).is_relative_to(config.data_dir())


def test_forgetting_a_document_forgets_its_reference(doc):
    _, doc_id = doc
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    assert registry.baseline_path(doc_id).is_file()
    registry.forget(doc_id)
    assert not registry.baseline_path(doc_id).exists()
```

Simplifier le troisième test à l'écriture : la branche `NothingToSend` se teste après avoir vidé les notes en attente (`registry.mark_sent` sur `n2`), puis `dispatch` doit lever `NothingToSend` et laisser `first` en place.

- [ ] **Step 2: les lancer, constater l'échec**

Run: `uv run pytest tests/test_baseline.py -q`
Expected: FAIL, `AttributeError: module 'annotate.registry' has no attribute 'read_baseline'`.

- [ ] **Step 3: implémenter**

Dans `registry.py`, docstring de disposition, ajouter la ligne :

```
    baselines/<id>.html      the document as it was at the last send (take_baseline)
```

Après `annotations_path` :

```python
def baseline_path(doc_id: str) -> Path:
    if not doc_id.isalnum():
        raise UnknownDocument(f"invalid document id {doc_id!r}")
    return _root() / "baselines" / f"{doc_id}.html"


def take_baseline(doc_id: str, stamp: str) -> str:
    """Copy the document as it is NOW: the reference the page compares with.

    Taken at every send, and by "Mark as read". One per document, replaced.
    When the copy cannot be made, the previous one is REMOVED and the reason
    returned (and kept in `last_error`): comparing with an older version
    would mark as new what the reader has already read. Never raises: at a
    send, the notes are already gone.
    """
    target = baseline_path(doc_id)
    try:
        text = Path(get(doc_id)["path"]).read_text(encoding="utf-8", errors="replace")
        atomic_write(target, text)
    except OSError as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        reason = f"no reference kept for what changes next: {exc}"
        update(doc_id, baseline_at="", last_error=reason[:500])
        return reason
    update(doc_id, baseline_at=stamp)
    return ""


def read_baseline(doc_id: str) -> str | None:
    try:
        return baseline_path(doc_id).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
```

Dans `forget`, à côté de `annotations_path(doc_id).unlink(missing_ok=True)` :

```python
        baseline_path(doc_id).unlink(missing_ok=True)
```

Dans `sender.py`, `_settle` devient :

```python
    def _settle(self, doc_id: str, items: list[dict[str, Any]], target: str,
                **fields: Any) -> None:
        stamp = _stamp()
        registry.mark_sent(doc_id, [a["id"] for a in items], stamp)
        registry.update(doc_id, status="delivered", sent_at=stamp, delivered_to=target,
                        last_error="", **fields)
        # After the update, so that its last_error="" never hides a failed copy.
        missing = registry.take_baseline(doc_id, stamp)
        if missing:
            log.warning("%s: %s", doc_id, missing)
```

- [ ] **Step 4: vert**

Run: `uv run pytest tests/test_baseline.py tests/test_sender.py tests/test_registry.py -q` → PASS.

- [ ] **Step 5: commit**

```bash
git add src/annotate/registry.py src/annotate/sender.py tests/test_baseline.py
git commit -m "feat: chaque envoi garde le document tel qu'il était, référence de ce qui change"
```

---

### Task 2: la route `/baseline`

**Files:**
- Modify: `src/annotate/server.py` (docstring des routes, `_read_document`, `_api`)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `registry.take_baseline`, `registry.read_baseline`.
- Produces: `GET /api/docs/<id>/baseline` → `200 {"reference": str, "current": str, "at": str}`, `404` sans référence, `410` si le fichier a disparu ; `POST /api/docs/<id>/baseline` → `200 {"at": str}`, `500` si la copie échoue.

- [ ] **Step 1: tests**

```python
def test_the_baseline_route_gives_both_versions_and_nothing_before_a_send(daemon, make_doc):
    path = make_doc("<p>as sent</p>")
    doc_id = registry.register(path, session="sess-1")["id"]
    status, body = daemon.request("GET", f"/api/docs/{doc_id}/baseline")
    assert status == 404 and "nothing was sent" in body["error"]
    sent = path.read_text()
    registry.take_baseline(doc_id, "T1")
    path.write_text(sent.replace("as sent", "corrected"))
    status, body = daemon.request("GET", f"/api/docs/{doc_id}/baseline")
    assert status == 200
    assert (body["reference"], body["at"]) == (sent, "T1")
    assert "corrected" in body["current"] and "annotate.js" not in body["current"]
    path.unlink()
    assert daemon.request("GET", f"/api/docs/{doc_id}/baseline")[0] == 410


def test_mark_as_read_needs_the_client_header_and_moves_the_reference(daemon, make_doc):
    path = make_doc("<p>first</p>")
    doc_id = registry.register(path)["id"]
    registry.take_baseline(doc_id, "T1")
    path.write_text(path.read_text().replace("first", "second"))
    status, _ = daemon.request("POST", f"/api/docs/{doc_id}/baseline",
                               headers={"X-Annotate": None})
    assert status == 403 and "first" in registry.read_baseline(doc_id)
    status, body = daemon.request("POST", f"/api/docs/{doc_id}/baseline")
    assert status == 200 and body["at"] and registry.get(doc_id)["baseline_at"] == body["at"]
    assert "second" in registry.read_baseline(doc_id)
```

Vérifier d'abord que `Daemon.request` sait retirer l'en-tête : `hdrs.update(headers)` met `None` ; si `http.client` refuse `None`, filtrer dans `request` les en-têtes valant `None` (une ligne : `hdrs = {k: v for k, v in hdrs.items() if v is not None}`).

- [ ] **Step 2: rouge** — `uv run pytest tests/test_server.py -q -k baseline` → FAIL (404 not found sur les deux).

- [ ] **Step 3: implémenter** — docstring :

```
    GET    /api/docs/<id>/baseline           the reference (the file at the last send)
                                             and the file now, raw: the page compares them
    POST   /api/docs/<id>/baseline           "Mark as read": the file now becomes the reference
```

Extraire la lecture de `_document` :

```python
    def _read_document(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise Refused(HTTPStatus.GONE, f"{path} no longer exists") from exc
        except OSError as exc:
            raise Refused(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from exc

    def _document(self, entry: dict[str, Any], path: Path) -> None:
        page = htmldoc.inject_overlay(self._read_document(path), entry["id"])
        self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
```

Dans `_api`, avant `raise Refused(... "not found")` :

```python
        if action == "baseline":
            return self._baseline(method, doc_id)
```

et la méthode :

```python
    def _baseline(self, method: str, doc_id: str) -> None:
        entry = registry.get(doc_id)
        if method == "GET":
            reference = registry.read_baseline(doc_id)
            if reference is None or not entry.get("baseline_at"):
                raise Refused(HTTPStatus.NOT_FOUND, "no reference: nothing was sent yet")
            current = self._read_document(Path(entry["path"]))
            return self._json(HTTPStatus.OK, {"reference": reference, "current": current,
                                              "at": entry["baseline_at"]})
        if method == "POST":
            stamp = registry.now_iso()
            missing = registry.take_baseline(doc_id, stamp)
            if missing:
                raise registry.RegistryError(missing)
            log.info("%s marked as read from the page", doc_id)
            return self._json(HTTPStatus.OK, {"at": stamp})
        raise Refused(HTTPStatus.METHOD_NOT_ALLOWED, "GET or POST")
```

- [ ] **Step 4: vert** — `uv run pytest -q` → PASS.

- [ ] **Step 5: commit** — `git commit -m "feat: la page lit la référence et le fichier actuel, et « Mark as read » avance la référence"`

---

### Task 3: `diff.js`, la comparaison pure

**Files:**
- Create: `src/annotate/static/diff.js`
- Modify: `src/annotate/htmldoc.py` (`inject_overlay` charge `diff.js` et `changes.js` avant `annotate.js`)
- Modify: `tests/test_htmldoc.py` (`test_inject_overlay_adds_base_and_script_once`)
- Create: `tests/browser/changes_check.mjs` (partie « pure »)
- Modify: `tests/test_browser.py` (`test_changes_in_a_real_browser`)

**Interfaces:**
- Produces: `window.__annotateDiff = { compare, myers, ownTextNodes, wordsOf, MAX_EDITS }`.
  - `compare(referenceHtml: string, currentHtml: string, live: Document) -> Result`
  - `Result = { tooMany: boolean, units: Unit[], changes: Change[], lost: number }`
  - `Unit = { sig, tag, text, words: string[], live: Element|null }` (unités du fichier actuel, ordre du document)
  - `Change = { kind: 'new'|'changed', unit: Unit, oldText?: string, added?: number[] }` ou `{ kind: 'removed', text: string, anchor: Unit|null, place: 'after'|'before' }`
  - `myers(a: string[], b: string[], max: number) -> Array<['='|'-'|'+', i, j]> | null`

- [ ] **Step 1: `diff.js`** — le fichier complet :

```js
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
```

- [ ] **Step 2: chargement** — dans `inject_overlay`, le bloc `overlay` devient :

```python
    overlay = (
        '\n<link rel="stylesheet" href="/static/annotate.css">'
        '\n<script src="/static/diff.js" defer></script>'
        '\n<script src="/static/changes.js" defer></script>'
        f'\n<script src="/static/annotate.js" data-annotate-doc="{quoted}" defer>'
        "</script>\n")
```

et `test_inject_overlay_adds_base_and_script_once` ajoute :

```python
    assert out.index("diff.js") < out.index("changes.js") < out.index("annotate.js")
```

`changes.js` n'existe qu'à la Task 4 : créer d'ici là un fichier minimal `src/annotate/static/changes.js` contenant `window.__annotateChanges = null;`, pour qu'aucune page ne charge un 404.

- [ ] **Step 3: contrôle pur dans un vrai navigateur** — `tests/browser/changes_check.mjs`, première partie (même en-tête que `overlay_check.mjs` ; arguments `<playwright-dir> <base-url> <doc-id> <out-dir> <doc-path>`). Après `page.goto` et l'attente de `annotate-root` :

```js
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
```

(L'exemple de Myers est celui de l'article d'origine : distance d'édition 5.)

Dans `tests/test_browser.py`, `test_changes_in_a_real_browser` : même ossature que `test_overlay_in_a_real_browser` (démon sur port 0, `RecordingOpener`, `subprocess.run([node, str(CHANGES_SCRIPT), pw_dir, base, doc_id, str(out), str(path)], timeout=180)`), document de la Task 4 ; à ce stade, un document quelconque suffit.

- [ ] **Step 4: vert** — `uv run pytest tests/test_htmldoc.py -q` puis `ANNOTATE_PLAYWRIGHT_DIR=/home/ali/.npm/_npx/f0a362733743bae2 uv run pytest -m browser -q` → PASS.

- [ ] **Step 5: commit** — `git commit -m "feat: comparaison bloc à bloc entre la référence et le fichier, dans le navigateur"`

---

### Task 4: `changes.js`, le dessin, et le branchement dans l'overlay

**Files:**
- Replace: `src/annotate/static/changes.js`
- Modify: `src/annotate/static/annotate.js` (état `changeView`, `loadChanges`, `markRead`, `render`, `renderBar`, Alt+clic, liens de fragment, épingles envoyées)
- Modify: `src/annotate/static/annotate.css`
- Modify: `tests/browser/changes_check.mjs`, `tests/test_browser.py`

**Interfaces:**
- Consumes: `__annotateDiff.compare`, `ownTextNodes`, `wordsOf`.
- Produces: `window.__annotateChanges = { apply }`, avec `apply(result, opts) -> View | null` ; `opts = { layer, at, flash, notes: [{target: Element|null, sent: boolean}], changed: () => void, markRead: () => void }` ; `View = { items, hidden(node), reveal(node), showAll(), next(), prev(), layout(origin), panel(), total }`. `apply` rend `null` quand il n'y a aucun changement (témoin) ; une vue sans items mais `tooMany` porte le message.

- [ ] **Step 1: le document du test** — dans `test_changes_in_a_real_browser`, version A écrite par `make_doc` :

```python
A = ("<h1>Changes check</h1><p>Intro paragraph stays. <a href='#ctx-two'>to context two</a></p>"
     "<h2 id='s1'>Context</h2><p>Context one.</p><p id='ctx-two'>Context two, already read.</p>"
     "<h2 id='s2'>Options</h2><p>Three options were compared.</p>"
     "<p>The migration takes a week, measured on the test copy.</p>"
     "<p>Option C needs one more server.</p>"
     "<h2 id='s3'>Notes</h2><p>A paragraph with an unsent note.</p>"
     "<h2 id='s4'>Decision</h2><p>Decision text.</p>"
     "<p id='clock'>Clock reads ten past nine</p>"
     "<script>document.getElementById('clock').textContent = 'Set by a script'</script>")
```

Puis : `registry.register(path, session="sess-browser")` ; note `n1` sur `body > p:nth-of-type(2)` (« Context one. »), envoyée par `srv.sender.dispatch(doc_id)` (le `RecordingOpener` prend l'onglet) : la référence est A. Note `n2`, non envoyée, sur `body > p:nth-of-type(7)`. Version B écrite sur le disque :

```python
B = A.replace("<p>The migration takes a week, measured on the test copy.</p>"
              "<p>Option C needs one more server.</p>",
              "<p>A fourth option keeps the old format read only for a month.</p>"
              "<p>The migration takes two days, measured on the test copy.</p>")
```

(B garde 8 `p` au même rang que A pour `n2` : « fourth option » prend la place 5, la migration la 6, et « Option C » disparaît.)

Après le script : `registry.read_baseline(doc_id)` doit contenir `ten past ten` (C, prise par « Mark as read »), et la note `n3` doit avoir le sélecteur `body > p:nth-of-type(7)`.

- [ ] **Step 2: le scénario navigateur** — à la suite de la partie pure de `changes_check.mjs` :

```js
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
  await page.evaluate(() => [...document.querySelector('annotate-root').shadowRoot
    .querySelectorAll('.chg-actions button')].find((b) => b.textContent === 'Mark as read').click());
  await page.waitForFunction(() => performance.getEntriesByType('navigation')[0] && document.readyState === 'complete');
  await page.waitForFunction(() => document.querySelector('annotate-root') &&
    document.querySelector('annotate-root').shadowRoot.querySelector('.bar').textContent.includes('to send'));
  const witness = await page.evaluate(() => document.querySelectorAll('.annotate-new,.annotate-changed,annotate-fold,.annotate-dim').length);
  if (witness || (await barText()).includes('Changes since')) fail('after Mark as read: ' + witness + ' marks');
  step('Mark as read: no mark, no fold, no panel');
  if (errors.length) fail('page errors: ' + errors.join(' | '));
```

(Imports en tête : `readFileSync`, `writeFileSync` de `node:fs`.)

- [ ] **Step 3: rouge** — `ANNOTATE_PLAYWRIGHT_DIR=… uv run pytest -m browser -q -k changes` → FAIL « changes panel shown » (attente expirée).

- [ ] **Step 4: `changes.js`** — le fichier complet :

```js
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
            const y = item.change.place === 'after' ? box.bottom : box.top;
            el.style.left = (box.right - origin.left - el.offsetWidth - 4) + 'px';
            el.style.top = (y - origin.top - 9) + 'px';
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
```

- [ ] **Step 5: `annotate.js`** — modifications :

1. Après `let saving = Promise.resolve();` : `let changeView = null;          // what changed since the last send (changes.js)`.
2. Dans le `mousedown` d'Alt+clic, après `if (!e.altKey || e.button !== 0 || isUi(e)) return;` :
   ```js
       if (e.target.closest && e.target.closest('annotate-fold')) return;
   ```
3. Dans `render()`, après `const box = visibleBox(target);` remplacer la pose de l'épingle pour masquer celles d'une section repliée, et marquer les envoyées :
   ```js
      pin.className = 'pin' + (item.sent_at ? ' sent' : '');
      pin.textContent = (item.sent_at ? '✓' : '') + String(item.number);
      pin.style.display = changeView && changeView.hidden(target) ? 'none' : '';
   ```
   et, avant `renderBar(orphans);` : `if (changeView) changeView.layout(origin);`.
4. Dans `renderBar`, le compteur : `pending + ' to send · ' + sent + ' sent' + (changeView ? ' · ' + changeView.total + ' changes' : '')`, et juste après `if (collapsed) return;` : `if (changeView) bar.append(changeView.panel());`.
5. Le gestionnaire des liens de fragment, avant `location.hash = …` :
   ```js
    const aimed = document.getElementById(decodeURIComponent(link.getAttribute('href').slice(1)));
    if (changeView && aimed) { changeView.reveal(aimed); render(); }
   ```
6. Les deux fonctions, après `refreshEntry` :
   ```js
  async function loadChanges() {
    const D = window.__annotateDiff;
    const C = window.__annotateChanges;
    if (!entry || !entry.baseline_at || !D || !C) return;
    let data;
    try {
      data = await call('GET', '/baseline');
    } catch (err) {
      message = 'Cannot compare with your last send: ' + err.message;
      return;
    }
    changeView = C.apply(D.compare(data.reference, data.current, document), {
      layer, at: data.at, flash,
      notes: annotations.map((a) => ({ target: resolve(a.selector), sent: Boolean(a.sent_at) })),
      changed: () => render(),
      markRead,
    });
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
   ```
7. Au démarrage, entre `await refreshEntry();` et `render();` : `await loadChanges();`.

- [ ] **Step 6: `annotate.css`** — en tête (côté document) :

```css
.annotate-new { background-color: rgba(22, 163, 74, .12) !important;
  box-shadow: inset 4px 0 0 #16a34a !important; }
.annotate-changed { background-color: rgba(37, 99, 235, .11) !important;
  box-shadow: inset 4px 0 0 #2563eb !important; }
tr.annotate-new, tr.annotate-changed { box-shadow: none !important; }
tr.annotate-new > :first-child { box-shadow: inset 4px 0 0 #16a34a !important; }
tr.annotate-changed > :first-child { box-shadow: inset 4px 0 0 #2563eb !important; }
svg.annotate-new, img.annotate-new { outline: 3px solid #16a34a !important; outline-offset: 4px; }
svg.annotate-changed, img.annotate-changed { outline: 3px solid #2563eb !important; outline-offset: 4px; }
.annotate-dim { opacity: .5; transition: opacity .15s; }
.annotate-dim:hover { opacity: 1; }
.annotate-folded { display: none !important; }
annotate-fold { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0 12px;
  padding: 8px 12px; border: 1px dashed rgba(127, 127, 127, .45); border-radius: 8px;
  background: rgba(127, 127, 127, .10); font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
annotate-fold b { font-weight: 600; }
annotate-fold span { opacity: .7; }
annotate-fold button { margin-left: auto; font: 600 13px system-ui, sans-serif; color: #2563eb;
  background: none; border: 0; cursor: pointer; padding: 2px 4px; }
::highlight(annotate-added) { background-color: rgba(37, 99, 235, .18); text-decoration: underline 2px #2563eb; }
```

Dans le bloc des variables `.layer, .bar` : `--new: #16a34a; --chg: #2563eb; --gone: #b42318;` (sombre : `--new: #4ade80; --chg: #7aa2ff; --gone: #f87171;`). Puis, côté couche :

```css
.pin.sent { min-width: 20px; height: 20px; font-size: 11px; opacity: .8; }
.chg-tag { position: absolute; font: 800 11px/16px system-ui, sans-serif; letter-spacing: .04em;
  color: #fff; padding: 0 7px; border-radius: 999px; pointer-events: auto; cursor: default; }
.chg-tag.new { background: var(--new); }
.chg-tag.changed { background: var(--chg); }
.chg-removed { position: absolute; font: 12px/18px system-ui, sans-serif; color: var(--gone);
  background: var(--paper); border: 1px solid var(--gone); border-radius: 999px; padding: 0 9px; cursor: pointer; }
.chg-old { position: absolute; max-width: 420px; padding: 6px 10px; background: var(--paper); color: var(--muted);
  border-left: 3px solid var(--gone); border-radius: 0 6px 6px 0; text-decoration: line-through;
  box-shadow: 0 4px 16px rgba(0, 0, 0, .15); font-size: 13px; }
.chg { margin: 8px 0 10px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; }
.chg-head { font-weight: 700; }
.chg-when { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
.chg-counts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 8px; }
.chg-counts div { border-radius: 7px; padding: 4px; text-align: center; font-size: 12px; color: var(--muted);
  background: color-mix(in srgb, currentColor 6%, transparent); }
.chg-counts b { display: block; font-size: 18px; line-height: 1.1; }
.chg-counts .new b { color: var(--new); }
.chg-counts .changed b { color: var(--chg); }
.chg-counts .removed b { color: var(--gone); }
.chg-lost { color: var(--danger); font-size: 12px; margin: 4px 0 8px; }
.chg-nav { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.chg-nav span { flex: 1; text-align: center; font-weight: 700; }
.chg-list { list-style: none; margin: 0 0 8px; padding: 0; max-height: 180px; overflow: auto; }
.chg-list li { display: flex; gap: 7px; padding: 4px 0; border-bottom: 1px solid var(--line); font-size: 12px; cursor: pointer; }
.chg-list li.cur { font-weight: 700; }
.chg-list .dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 4px; flex: none; }
.chg-list .dot.new { background: var(--new); }
.chg-list .dot.changed { background: var(--chg); }
.chg-list .dot.removed { background: var(--gone); }
.chg-actions { display: flex; flex-wrap: wrap; gap: 6px; }
```

- [ ] **Step 7: vert, puis regarder** — `uv run pytest -q`, `ANNOTATE_PLAYWRIGHT_DIR=… uv run pytest -m browser -q`, `uv run ruff check src tests tools hooks`, `uv run mypy`. Ouvrir `changes-open.png` (dossier `ANNOTATE_BROWSER_OUT`), et un rendu sombre (`colorScheme: 'dark'`) du même document.

- [ ] **Step 8: commit** — `git commit -m "feat: la page met en avant ce qui a changé depuis le dernier envoi et replie le déjà-lu"`

---

### Task 5: mutations, invariants, vitrine

**Files:**
- Modify: `tools/mutate.py`, `CLAUDE.md`, `README.md`, `ROADMAP.md`

- [ ] **Step 1: mutations** — ajouter à `MUTATIONS` :

```python
    Mutation("baseline-not-taken", "src/annotate/sender.py",
             "        missing = registry.take_baseline(doc_id, stamp)\n",
             "        missing = ''\n",
             "every send keeps the document as the reference"),
    Mutation("baseline-kept-on-failure", "src/annotate/registry.py",
             "            target.unlink(missing_ok=True)\n",
             "            pass\n",
             "a reference that cannot be written is removed"),
    Mutation("pairing-unchecked", "src/annotate/static/diff.js",
             "jaccard(old.words, unit.words) >= PAIR_MIN",
             "jaccard(old.words, unit.words) >= 0",
             "only a close block counts as changed", browser=True),
    Mutation("fold-hides-unsent", "src/annotate/static/changes.js",
             "        if (!changedHere && !unsent) fold(sec);\n",
             "        if (!changedHere) fold(sec);\n",
             "a section with an unsent note never folds", browser=True),
    Mutation("mark-unverified", "src/annotate/static/diff.js",
             "      unit.live = node && signature(node) === unit.sig ? node : null;\n",
             "      unit.live = node;\n",
             "a block is marked only where the page shows the same text", browser=True),
    Mutation("fold-shifts-anchors", "src/annotate/static/changes.js",
             "      const bar = document.createElement('annotate-fold');\n",
             "      const bar = document.createElement('p');\n",
             "folding never moves a pin anchor", browser=True),
```

`fold-shifts-anchors` : avec un `p`, la note posée à l'étape 6 prend `nth-of-type(8)` et le contrôle Python du sélecteur `n3` tombe.

- [ ] **Step 2: campagne** — `ANNOTATE_PLAYWRIGHT_DIR=… uv run python tools/mutate.py baseline-not-taken baseline-kept-on-failure pairing-unchecked fold-hides-unsent mark-unverified fold-shifts-anchors` : chacune doit faire tomber la suite (si l'outil ne prend qu'un nom, les lancer une à une).

- [ ] **Step 3: `CLAUDE.md`** — dans le schéma de tête rien ne change ; ajouter après le paragraphe sur la page d'accueil :

> **La page met en avant ce qui a changé depuis le dernier envoi** (2026-10-12, spec `docs/2026-10-12-mise-en-avant-des-mises-a-jour.html`). Chaque envoi copie le fichier dans `baselines/<id>.html` (`registry.take_baseline`, appelé par `Sender._settle`) ; la page compare cette copie au fichier (`static/diff.js`), marque le nouveau et le modifié, replie les sections inchangées (`static/changes.js`). « Mark as read » avance la référence.

et à la table des invariants :

| la copie de référence est prise à chaque envoi, jamais à côté du document ; un échec la retire | `test_a_send_keeps_the_document_as_it_was_as_the_reference`, `test_a_reference_that_cannot_be_written_is_removed_and_said`, `test_the_reference_never_lives_next_to_the_document` |
| référence identique → aucune marque ; ajout, modification, retrait → exactement une marque chacun ; une section avec une note non envoyée ne se replie pas ; le repli ne déplace aucune ancre | `tests/browser/changes_check.mjs` |

Et dans « Ce qui n'est pas évident » : la ligne sur `<annotate-fold>` et `nth-of-type`.

- [ ] **Step 4: vitrine** — `README.md` : une phrase sous la description de l'overlay ; `ROADMAP.md` : l'entrée passe en « done ». En anglais.

- [ ] **Step 5: tout repasser, commit, push** — suite par défaut, navigateur, ruff, mypy ; `git commit -m "test: mutations et invariants de la mise en avant des changements"` ; `git push`.
