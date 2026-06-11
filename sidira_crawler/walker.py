"""Playwright walker — generic walk engine that explores a web app and records observations.

The walker is a GENERIC engine: it knows nothing about any specific customer's
markup, routes, copy, or test ids. It explores from an entry URL using only
generic heuristics (consent-overlay detection, login-form detection, chooser
interstitials, lazy-feed scrolling, dialog detection) and records a raw graph
of screens and transitions. Synthesis modules turn that graph into
reverse-PRD + reverse-Figma.

Core semantics
--------------
- Depth = number of user-action transitions from the entry URL. Modal opens,
  wizard steps, and auth round-trips each count as ONE hop.
- A screen is (normalized route pattern + UI state signature). Likely-ID path
  segments are collapsed (``/title/t-101`` == ``/title/t-205``). The state
  signature combines boolean flags derived from DOM landmarks (dialog present,
  blocking overlay present, login form present, chooser interstitial,
  feed-exhausted) with a short structural digest (testid shapes, landmark
  roles, form-control types, bucketed heading count, real-button action
  texts) so same-route structural states — wizard steps, tab panels, drawers,
  in-place confirm stages — are distinct screens while same-template pages
  with different content still collapse.
- Exploration is breadth-first with deterministic, document-order action
  enumeration. Two identical walks produce identical screens and transitions.
"""

from __future__ import annotations

import hashlib
import os
import re
import weakref
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import structlog
from playwright.sync_api import Browser, Error, Page

from sidira_crawler.config import CapabilityConfig

log = structlog.get_logger()

#: Default depth budget (user-action hops from the entry URL).
DEFAULT_DEPTH = 6
#: Hard cap on scroll rounds per screen — guarantees termination on any feed.
SCROLL_HARD_CAP = 30
#: Consecutive no-growth scroll attempts before we declare the feed settled.
SCROLL_STALL_LIMIT = 3
#: Max candidate actions attempted per screen.
MAX_CANDIDATES_PER_SCREEN = 12
#: Max candidates tried per similar-element group (e.g. repeated feed cards).
MAX_PER_SIMILAR_GROUP = 2
#: Max visible-copy length (chars) for a screen to qualify as a chooser interstitial.
CHOOSER_MAX_TEXT = 600


@dataclass
class RawObservation:
    """One observed screen during a walk.

    Every list/string is bounded so observations stay small enough to feed to
    synthesis (and to be mapped onto an external answer key) without the
    walker knowing anything about that key.
    """

    url: str
    route: str
    title: str
    role: str
    screen_id: str
    headings: list[str] = field(default_factory=list)
    dom_outline: str = ""
    visible_copy: list[str] = field(default_factory=list)
    data_testids: list[str] = field(default_factory=list)
    landmarks: list[str] = field(default_factory=list)
    has_dialog: bool = False
    has_blocking_overlay: bool = False


@dataclass
class RawTransition:
    """One observed transition between screens."""

    from_screen_id: str
    to_screen_id: str
    trigger: str
    role: str


@dataclass
class RawWalk:
    """Output of a single role's walk through a single capability."""

    capability_name: str
    role: str
    observations: list[RawObservation] = field(default_factory=list)
    transitions: list[RawTransition] = field(default_factory=list)


# --------------------------------------------------------------------------
# In-page JavaScript (generic heuristics only — no customer selectors)
# --------------------------------------------------------------------------

_JS_HELPERS = """
  const __vis = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const __live = (el) => !!(
    el.closest && el.closest('[aria-live], [role="status"], [role="alert"]')
  );
"""

# Set an input's value via the PROTOTYPE's native value setter, not
# `el.value = v`. Frameworks with controlled inputs (React et al.) install an
# instance-level value tracker; a direct assignment updates the tracker too,
# so the framework sees "no change" on the input event and never updates its
# state — the form then submits empty fields. The prototype setter bypasses
# the instance tracker, so the dispatched input event registers as a real edit.
_JS_SETVAL = """
  const __setVal = (el, v) => {
    el.focus();
    const desc = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(el).constructor.prototype, 'value');
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
"""

# Deterministic synthetic values for empty text-like inputs, filled before a
# button-type candidate is clicked so required-field validation cannot
# silently block exploration (wizard steps, gated submits). Same values every
# run — determinism is part of the walker contract. Credentialed login walls
# never reach this path: the login handler takes precedence in `expand`.
_JS_FILL = """
  const __fillValue = (inp) => {
    if (inp.tagName === 'TEXTAREA') return 'walker';
    const type = (inp.getAttribute('type') || 'text').toLowerCase();
    const values = {email: 'walker@example.com', password: 'walker-password-1',
                    text: 'walker', search: 'walker', tel: '5550100', number: '1'};
    return type in values ? values[type] : null;
  };
  const __fillEmpty = (scope) => {
    for (const inp of scope.querySelectorAll('input, textarea')) {
      if (!__vis(inp) || __live(inp)) continue;
      if (inp.disabled || inp.readOnly || inp.value) continue;
      const v = __fillValue(inp);
      if (v !== null) __setVal(inp, v);
    }
  };
"""

_JS_CAPTURE = (
    "() => {"
    + _JS_HELPERS
    + """
  const headings = [];
  for (const h of document.querySelectorAll('h1, h2, h3')) {
    if (!__vis(h) || __live(h)) continue;
    const t = (h.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
    if (t) headings.push(t);
    if (headings.length >= 10) break;
  }
  const tids = new Set();
  for (const el of document.querySelectorAll('[data-testid]')) {
    if (__live(el)) continue;
    tids.add(el.getAttribute('data-testid'));
    if (tids.size >= 50) break;
  }
  const landmarks = new Set();
  const tagRoles = {HEADER: 'banner', NAV: 'navigation', MAIN: 'main',
                    FOOTER: 'contentinfo', ASIDE: 'complementary', FORM: 'form'};
  for (const el of document.querySelectorAll('header, nav, main, footer, aside, form, [role]')) {
    if (!__vis(el) || __live(el)) continue;
    const role = el.getAttribute('role') || tagRoles[el.tagName];
    if (role) landmarks.add(role);
  }
  const controls = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (!__vis(el) || __live(el)) continue;
    if (el.tagName === 'SELECT') controls.push('select');
    else if (el.tagName === 'TEXTAREA') controls.push('textarea');
    else controls.push('input:' + (el.getAttribute('type') || 'text').toLowerCase());
    if (controls.length >= 50) break;
  }
  controls.sort();
  // Action affordances: visible texts of REAL button elements only. Elements
  // with role="button" are excluded on purpose — content cards commonly use
  // that role, and their text varies with content (which must collapse).
  const actions = new Set();
  for (const b of document.querySelectorAll(
      'button, input[type="submit"], input[type="button"]')) {
    if (!__vis(b) || __live(b)) continue;
    const t = (b.textContent || b.value || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    if (t) actions.add(t);
    if (actions.size >= 25) break;
  }
  const copy = [];
  const seen = new Set();
  if (document.body) {
    const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = tw.nextNode()) && copy.length < 40) {
      const p = n.parentElement;
      if (!p || !__vis(p) || __live(p)) continue;
      if (p.closest('script, style, noscript')) continue;
      const t = (n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
      if (t.length < 2 || seen.has(t)) continue;
      seen.add(t);
      copy.push(t);
    }
  }
  const lines = [];
  const outline = (el, d) => {
    if (lines.length >= 60 || d > 4) return;
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return;
    let line = '  '.repeat(d) + tag.toLowerCase();
    const r = el.getAttribute('role');
    if (r) line += '[role=' + r + ']';
    const tid = el.getAttribute('data-testid');
    if (tid) line += '[data-testid=' + tid + ']';
    lines.push(line);
    for (const c of el.children) outline(c, d + 1);
  };
  if (document.body) outline(document.body, 0);
  let hasDialog = false;
  for (const d of document.querySelectorAll(
      'dialog[open], [role="dialog"], [aria-modal="true"]')) {
    if (__vis(d)) { hasDialog = true; break; }
  }
  let hasPassword = false;
  for (const i of document.querySelectorAll('input[type="password"]')) {
    if (__vis(i)) { hasPassword = true; break; }
  }
  const copyLen = copy.reduce((a, t) => a + t.length, 0);
  return {title: document.title || '', headings: headings,
          data_testids: Array.from(tids).sort(), landmarks: Array.from(landmarks).sort(),
          form_controls: controls, action_texts: Array.from(actions).sort(),
          visible_copy: copy, dom_outline: lines.join('\\n'),
          has_dialog: hasDialog, has_password: hasPassword, copy_len: copyLen};
}"""
)

# Finds a blocking overlay: a visible position:fixed element covering >= 25%
# of the viewport that wins the hit test at its own center. If it contains an
# affirmative consent-style button, optionally clicks it (arg = doClick).
_JS_OVERLAY = (
    "(doClick) => {"
    + _JS_HELPERS
    + """
  const vw = window.innerWidth, vh = window.innerHeight;
  let overlay = null;
  for (const el of document.querySelectorAll('body *')) {
    if (!__vis(el) || __live(el)) continue;
    if (getComputedStyle(el).position !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    const w = Math.min(r.right, vw) - Math.max(r.left, 0);
    const h = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (w <= 0 || h <= 0 || (w * h) < vw * vh * 0.25) continue;
    const cx = Math.max(0, Math.min(r.left + r.width / 2, vw - 1));
    const cy = Math.max(0, Math.min(r.top + r.height / 2, vh - 1));
    const top = document.elementFromPoint(cx, cy);
    if (!top || (top !== el && !el.contains(top))) continue;
    overlay = el;
    break;
  }
  if (!overlay) return null;
  const isDialog = !!overlay.closest('dialog, [role="dialog"], [aria-modal="true"]');
  const verbs =
    /\\b(accept|agree|allow|got it|ok|okay|continue|i understand|understood|consent|yes)\\b/i;
  let btn = null;
  for (const b of overlay.querySelectorAll(
      'button, [role="button"], a, input[type="button"], input[type="submit"]')) {
    if (!__vis(b)) continue;
    const t = ((b.textContent || '') + ' ' + (b.value || '')).replace(/\\s+/g, ' ').trim();
    if (verbs.test(t)) { btn = b; break; }
  }
  const text = btn
    ? ((btn.textContent || btn.value || '').replace(/\\s+/g, ' ').trim().slice(0, 60))
    : null;
  if (doClick && btn) btn.click();
  return {is_dialog: isDialog, has_button: !!btn, button_text: text};
}"""
)

# Shared candidate enumeration: links with same-origin hrefs, buttons,
# role=button elements, submit inputs — in document order, visible only,
# excluding live regions. When a dialog is open, enumerate inside it only.
_JS_ENUM = """
  const __collect = (dialogOnly) => {
    let root = document;
    if (dialogOnly) {
      for (const d of document.querySelectorAll(
          'dialog[open], [role="dialog"], [aria-modal="true"]')) {
        if (__vis(d)) { root = d; break; }
      }
    }
    const cands = [];
    const els = [];
    const sel = 'a[href], button, [role="button"], input[type="submit"], input[type="button"]';
    for (const el of root.querySelectorAll(sel)) {
      if (!__vis(el) || __live(el)) continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      let href = null;
      if (el.tagName === 'A') {
        if (el.getAttribute('target') === '_blank' || el.hasAttribute('download')) continue;
        const raw = el.getAttribute('href') || '';
        if (/^(javascript:|mailto:|tel:)/i.test(raw)) continue;
        let u;
        try { u = new URL(el.href, location.href); } catch (e) { continue; }
        if (u.origin !== location.origin) continue;
        // Hash ROUTES (#/... or #!/...) are real SPA navigation; plain
        // in-page #anchors are not candidates at all.
        const hashRoute = /^#!?\\//.test(u.hash);
        if (raw.startsWith('#') && !hashRoute) continue;
        href = u.pathname + u.search + (hashRoute ? u.hash : '');
      }
      const text = (el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 60);
      cands.push({tag: el.tagName.toLowerCase(), text: text, href: href,
                  role: el.getAttribute('role') || '',
                  classes: Array.from(el.classList).sort().join('.'),
                  testid: el.getAttribute('data-testid') || ''});
      els.push(el);
    }
    return {cands: cands, els: els, root: root};
  };
"""

_JS_CANDIDATES = (
    "(dialogOnly) => {" + _JS_HELPERS + _JS_ENUM + " return __collect(dialogOnly).cands; }"
)

# Clicks one candidate. args = {index, dialog, sig, ordinal}. When a
# signature is given the candidate is RE-RESOLVED in the live DOM at click
# time — the element whose (tag, text, href, role, classes, testid) identity
# matches, taking the ordinal-th duplicate in document order. Bare indices
# into a re-collected list go stale whenever an earlier candidate mutated the
# page in place (dismissed banner, removed row): the index then silently hits
# the WRONG element. Ambiguity (signature missing from the live DOM) returns
# false so the walker restores instead of guessing.
_JS_CLICK = (
    "(args) => {"
    + _JS_HELPERS
    + _JS_ENUM
    + _JS_SETVAL
    + _JS_FILL
    + """
  const __key = (c) => [c.tag, c.text, c.href, c.role, c.classes, c.testid].join('\\u001f');
  const resolve = () => {
    const got = __collect(args.dialog);
    if (!args.sig) return {el: got.els[args.index] || null, root: got.root};
    const want = __key(args.sig);
    let seen = 0;
    for (let i = 0; i < got.cands.length; i++) {
      if (__key(got.cands[i]) !== want) continue;
      if (seen === (args.ordinal || 0)) return {el: got.els[i], root: got.root};
      seen++;
    }
    return {el: null, root: got.root};
  };
  let found = resolve();
  let el = found.el;
  if (!el) return false;
  // Button-type candidates first fill every empty text-like input in scope
  // (the button's enclosing form when one exists, else the enumeration root)
  // so required-field validation cannot silently swallow the click. Filling
  // can re-render frameworks' DOM, so the candidate is resolved again after.
  if (el.tagName !== 'A') {
    __fillEmpty(el.form || el.closest('form') || found.root);
    const again = resolve();
    if (again.el) el = again.el;
    else if (!el.isConnected) return false;
  }
  el.click();
  return true;
}"""
)

# Fills the first visible login form (username/email + password) and submits.
_JS_LOGIN = (
    "(args) => {"
    + _JS_HELPERS
    + _JS_SETVAL
    + """
  const username = args[0], password = args[1];
  const pw = Array.from(document.querySelectorAll('input[type="password"]')).find(__vis);
  if (!pw) return false;
  const scope = pw.form || document;
  const userSel = 'input[type="email"], input[autocomplete="username"], ' +
                  'input[type="text"], input:not([type])';
  const uf = Array.from(scope.querySelectorAll(userSel)).find((el) => __vis(el) && el !== pw);
  if (uf) __setVal(uf, username);
  __setVal(pw, password);
  let btn = null;
  for (const b of scope.querySelectorAll('button[type="submit"], input[type="submit"], button')) {
    if (!__vis(b)) continue;
    if (b.tagName === 'BUTTON' && b.getAttribute('type') === 'button') continue;
    btn = b;
    break;
  }
  if (btn) btn.click();
  else if (pw.form) {
    if (pw.form.requestSubmit) pw.form.requestSubmit(); else pw.form.submit();
  }
  return true;
}"""
)

# Scroll metrics + a signature of the trailing in-flow element (used to detect
# "feed exhausted, terminal marker appeared" generically). Fixed-position
# elements (overlays, toasts, sticky footers) are excluded from the tail.
_JS_SCROLL_METRICS = (
    "() => {"
    + _JS_HELPERS
    + """
  const root = document.scrollingElement || document.documentElement;
  const inFixed = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (getComputedStyle(n).position === 'fixed') return true;
    }
    return false;
  };
  let tail = '';
  const all = document.body ? document.body.querySelectorAll('*') : [];
  for (let i = all.length - 1; i >= 0; i--) {
    const el = all[i];
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') continue;
    if (__live(el) || !__vis(el) || inFixed(el)) continue;
    tail = tag.toLowerCase() + '|' + (el.getAttribute('data-testid') || '') + '|' +
           (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    break;
  }
  const count = document.body ? document.body.getElementsByTagName('*').length : 0;
  return {scroll_height: root.scrollHeight, client_height: root.clientHeight,
          node_count: count, tail: tail};
}"""
)

_JS_SCROLL_TO_BOTTOM = (
    "() => window.scrollTo(0, (document.scrollingElement || document.documentElement).scrollHeight)"
)


# --------------------------------------------------------------------------
# URL / screen identity
# --------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")


def _looks_like_id(segment: str) -> bool:
    """Heuristic: path segments containing digits / UUIDs / long hex are IDs."""
    if _UUID_RE.match(segment) or _HEX_RE.match(segment):
        return True
    return any(ch.isdigit() for ch in segment)


def _normalize_path(path: str) -> str:
    segments = [s for s in path.split("/") if s]
    return "/".join(":id" if _looks_like_id(s) else s for s in segments)


def _normalize_route(url: str) -> str:
    """Collapse likely-ID path segments so ``/title/t-101`` == ``/title/t-205``.

    Query strings and plain fragments are dropped — UI-state differences they
    encode are captured by the state signature instead. Hash ROUTES
    (``#/...`` or ``#!/...``) are real navigation in hash-router SPAs, so
    such fragment paths are folded into the route with the same ``:id``
    segment normalization.
    """
    parts = urlsplit(url)
    route = "/" + _normalize_path(parts.path)
    fragment = parts.fragment
    bang = fragment.startswith("!")
    frag_path = (fragment[1:] if bang else fragment).split("?")[0]
    if frag_path.startswith("/"):
        route += ("#!/" if bang else "#/") + _normalize_path(frag_path)
    return route


_DIGIT_RUN_RE = re.compile(r"\d+")


def _heading_bucket(count: int) -> str:
    """Bucket the heading count so lazy-load growth of repeated content does
    not (usually) fork screen identity, while 1-heading vs many-heading
    layouts still differ."""
    if count <= 2:
        return f"h{count}"
    if count <= 5:
        return "h3-5"
    return "h6+"


def _structural_digest(
    data_testids: list[str],
    landmarks: list[str],
    form_controls: list[str],
    heading_count: int,
    action_texts: list[str],
) -> str:
    """Short deterministic digest of page STRUCTURE — never content text.

    Built so that the same template with different content collapses
    (``/items/:id`` pages, repeated feed cards) but same-route structural
    states (wizard steps, tab panels, drawers, in-place confirm stages)
    split:

    - sorted *set* of data-testid values with digit runs stripped (repeated
      cards and numbered ids collapse),
    - sorted set of ARIA landmark roles,
    - sorted multiset of visible form-control types,
    - bucketed heading count (growth of repeated cards usually stays in
      bucket; a crossing is bounded noise, which beats missed states),
    - sorted *set* of real-button action texts, digit runs stripped. Button
      labels are the page's action affordances — an in-place stage swap that
      replaces "Upgrade" with "Confirm"/"Cancel" is a different screen even
      when no testid, landmark, control, or heading changes. role="button"
      content cards are excluded (see _JS_CAPTURE) so feed growth still
      collapses.
    """
    testid_shapes = sorted({_DIGIT_RUN_RE.sub("", t) for t in data_testids})
    action_shapes = sorted({_DIGIT_RUN_RE.sub("", t) for t in action_texts})
    payload = "\x1f".join(
        [
            "|".join(testid_shapes),
            "|".join(sorted(set(landmarks))),
            "|".join(sorted(form_controls)),
            _heading_bucket(heading_count),
            "|".join(action_shapes),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:6]


@dataclass
class _PageState:
    """Internal snapshot of the live page, used for identity + classification."""

    url: str
    route: str
    title: str
    headings: list[str]
    dom_outline: str
    visible_copy: list[str]
    data_testids: list[str]
    landmarks: list[str]
    form_controls: list[str]
    action_texts: list[str]
    has_dialog: bool
    has_blocking_overlay: bool
    has_password: bool
    is_chooser: bool
    exhausted: bool
    copy_len: int
    candidates: list[dict]
    consent_button: str | None

    @property
    def screen_id(self) -> str:
        tokens = []
        if self.has_blocking_overlay:
            tokens.append("overlay")
        if self.has_dialog:
            tokens.append("dialog")
        if self.has_password:
            tokens.append("login")
        if self.is_chooser:
            tokens.append("chooser")
        if self.exhausted:
            tokens.append("exhausted")
        tokens.append(
            "s"
            + _structural_digest(
                self.data_testids,
                self.landmarks,
                self.form_controls,
                len(self.headings),
                self.action_texts,
            )
        )
        return f"{self.route}::{'+'.join(tokens)}"


@dataclass(frozen=True)
class _Node:
    """A discovered screen queued for BFS expansion."""

    screen_id: str
    url: str
    has_dialog: bool
    has_overlay: bool
    exhausted: bool
    path: tuple[dict, ...]
    depth: int


def _expansion_key(state: _PageState) -> str:
    """Dedupe key for BFS *expansion* (distinct from observation dedupe).

    Ordinary screens are expanded once per ``screen_id``. Continuation-bearing
    states — login walls and chooser interstitials — are pass-through screens
    whose onward behavior depends on the query string (e.g. ``?next=/a`` vs
    ``?next=/b``), so they are expanded once per (screen_id, full URL). The
    set of full URLs is bounded by the BFS depth and per-screen candidate
    caps, and enumeration order is deterministic, so this stays bounded and
    reproducible.
    """
    if state.has_password or state.is_chooser:
        return f"{state.screen_id}\x1f{state.url}"
    return state.screen_id


# --------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------


def _eval(page: Page, expression: str, arg: object = None, attempts: int = 3) -> object:
    """page.evaluate with retries — navigations can destroy the JS context."""
    for i in range(attempts):
        try:
            return page.evaluate(expression, arg)
        except Error:
            if i == attempts - 1:
                return None
            page.wait_for_timeout(150)
    return None


#: In-flight request tracking per page (see _track_network). Weak keys so
#: closed pages don't accumulate.
_PENDING_REQUESTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

#: Hard cap on _settle's stability loop (rounds of ~80ms). Generous enough to
#: outlast realistic in-app fetch chains, small enough to terminate on
#: pathological never-ending connections (long-poll, SSE).
_SETTLE_MAX_ROUNDS = 60


def _track_network(page: Page) -> set:
    """Track the page's in-flight requests (idempotent per page).

    ``wait_for_load_state("networkidle")`` is useless after same-document SPA
    actions — the state was already reached once, so the call returns
    immediately even while a click handler's fetch is still pending. The
    walker instead counts requests itself and lets _settle wait for true
    quiescence, otherwise an async click handler (fetch then client-side
    navigate) looks like a no-op at capture time and its deferred effect gets
    misattributed to the NEXT candidate's transition.
    """
    pending = _PENDING_REQUESTS.get(page)
    if pending is None:
        pending = set()
        _PENDING_REQUESTS[page] = pending

        # Plain functions, not pending.add/.discard: Playwright wraps event
        # handlers and cannot setattr on builtin bound methods.
        def _began(request: object) -> None:
            pending.add(request)

        def _ended(request: object) -> None:
            pending.discard(request)

        page.on("request", _began)
        page.on("requestfinished", _ended)
        page.on("requestfailed", _ended)
    return pending


def _settle(page: Page) -> None:
    """Wait for client render: load states, tracked network quiescence, and a
    DOM-stability heuristic. Bounded — never waits forever."""
    pending = _track_network(page)
    try:
        page.wait_for_load_state("load", timeout=5000)
    except Error:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=2000)
    except Error:
        pass
    last = -1
    stable = 0
    for _ in range(_SETTLE_MAX_ROUNDS):
        size = _eval(page, "() => document.body ? document.body.innerHTML.length : -1", attempts=1)
        if not pending and size is not None and size == last and size >= 0:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = size if size is not None else -1
        page.wait_for_timeout(80)


def _goto(page: Page, url: str) -> None:
    _track_network(page)
    page.goto(url, wait_until="load", timeout=15000)
    _settle(page)


def _scroll_page(page: Page) -> tuple[bool, int]:
    """Incrementally scroll to trigger lazy loads; bounded by structure.

    Keeps scrolling while content grows. Terminates after SCROLL_STALL_LIMIT
    consecutive no-growth attempts or SCROLL_HARD_CAP rounds. Returns
    ``(exhausted, rounds)`` where ``exhausted`` means: scrolling terminated
    because content stopped growing AND a new trailing in-flow element
    appeared since first paint (a terminal/empty-state marker, generically).
    """
    metrics = _eval(page, _JS_SCROLL_METRICS)
    if not metrics:
        return False, 0
    if metrics["scroll_height"] <= metrics["client_height"] + 4:
        return False, 0
    initial_tail = metrics["tail"]
    last = (metrics["scroll_height"], metrics["node_count"])
    grew_any = False
    stalls = 0
    rounds = 0
    while rounds < SCROLL_HARD_CAP and stalls < SCROLL_STALL_LIMIT:
        rounds += 1
        _eval(page, _JS_SCROLL_TO_BOTTOM)
        grew = False
        for _ in range(4):
            page.wait_for_timeout(90)
            metrics = _eval(page, _JS_SCROLL_METRICS)
            if metrics and (metrics["scroll_height"], metrics["node_count"]) != last:
                last = (metrics["scroll_height"], metrics["node_count"])
                grew = True
                break
        if grew:
            grew_any = True
            stalls = 0
        else:
            stalls += 1
    metrics = _eval(page, _JS_SCROLL_METRICS)
    exhausted = bool(
        stalls >= SCROLL_STALL_LIMIT
        and grew_any
        and metrics
        and metrics["tail"] != initial_tail
    )
    return exhausted, rounds


def _chooser_group(candidates: list[dict], copy_len: int) -> list[int] | None:
    """Detect a chooser-style interstitial: a small group of similar clickable
    items (non-link) and little else. Returns candidate indices of the group."""
    if copy_len > CHOOSER_MAX_TEXT or not candidates:
        return None
    groups: dict[tuple, list[int]] = {}
    for i, cand in enumerate(candidates):
        groups.setdefault((cand["tag"], cand["classes"], cand["role"]), []).append(i)
    key, idxs = max(groups.items(), key=lambda kv: len(kv[1]))
    if key[0] == "a":
        return None
    if not (2 <= len(idxs) <= 8):
        return None
    if len(candidates) - len(idxs) > 2:
        return None
    return idxs


def _capture_state(page: Page, exhausted: bool = False) -> _PageState:
    """Observe the live page: identity, landmarks, flags, candidates."""
    data = _eval(page, _JS_CAPTURE)
    if not data:
        raise RuntimeError(f"could not observe page at {page.url}")
    overlay = _eval(page, _JS_OVERLAY, False)
    candidates = _eval(page, _JS_CANDIDATES, bool(data["has_dialog"])) or []
    has_overlay = bool(overlay and not overlay["is_dialog"])
    consent_button = overlay["button_text"] if (overlay and overlay["has_button"]) else None
    is_chooser = False
    if not (has_overlay or consent_button or data["has_dialog"] or data["has_password"]):
        is_chooser = _chooser_group(candidates, data["copy_len"]) is not None
    url = page.url
    return _PageState(
        url=url,
        route=_normalize_route(url),
        title=data["title"],
        headings=data["headings"],
        dom_outline=data["dom_outline"],
        visible_copy=data["visible_copy"],
        data_testids=data["data_testids"],
        landmarks=data["landmarks"],
        form_controls=data["form_controls"],
        action_texts=data["action_texts"],
        has_dialog=bool(data["has_dialog"]),
        has_blocking_overlay=has_overlay,
        has_password=bool(data["has_password"]),
        is_chooser=is_chooser,
        exhausted=exhausted,
        copy_len=data["copy_len"],
        candidates=candidates,
        consent_button=consent_button,
    )


def _to_observation(state: _PageState, role: str) -> RawObservation:
    return RawObservation(
        url=state.url,
        route=state.route,
        title=state.title,
        role=role,
        screen_id=state.screen_id,
        headings=state.headings,
        dom_outline=state.dom_outline,
        visible_copy=state.visible_copy,
        data_testids=state.data_testids,
        landmarks=state.landmarks,
        has_dialog=state.has_dialog,
        has_blocking_overlay=state.has_blocking_overlay,
    )


def _sample_candidates(candidates: list[dict]) -> list[tuple[int, dict]]:
    """Bound the action list: cap per similar-element group and overall.

    Group key includes href so navigation links with distinct targets are all
    kept, while repeated structural twins (feed cards) are sampled.
    """
    counts: dict[tuple, int] = {}
    sampled: list[tuple[int, dict]] = []
    for i, cand in enumerate(candidates):
        key = (cand["tag"], cand["classes"], cand["role"], cand["href"])
        n = counts.get(key, 0)
        if n >= MAX_PER_SIMILAR_GROUP:
            continue
        counts[key] = n + 1
        sampled.append((i, cand))
        if len(sampled) >= MAX_CANDIDATES_PER_SCREEN:
            break
    return sampled


def _execute_action(page: Page, action: dict, username: str | None, password: str | None) -> None:
    """Re-execute one recorded action during path replay. Deterministic."""
    kind = action["kind"]
    if kind == "dismiss":
        _eval(page, _JS_OVERLAY, True)
    elif kind == "login":
        _eval(page, _JS_LOGIN, [username or "", password or ""])
    elif kind == "choose_first":
        data = _eval(page, _JS_CAPTURE)
        cands = _eval(page, _JS_CANDIDATES, False) or []
        idxs = _chooser_group(cands, data["copy_len"]) if data else None
        _eval(page, _JS_CLICK, {"index": idxs[0] if idxs else 0, "dialog": False})
    elif kind == "scroll":
        _scroll_page(page)
    elif kind == "click":
        # Clicks are recorded against the post-scroll candidate list, so the
        # lazy content must be loaded before re-enumerating.
        _scroll_page(page)
        _eval(
            page,
            _JS_CLICK,
            {
                "index": action["index"],
                "dialog": action.get("dialog", False),
                "sig": action.get("sig"),
                "ordinal": action.get("ordinal", 0),
            },
        )
    _settle(page)


def _replay(
    page: Page,
    entry_url: str,
    path: tuple[dict, ...],
    username: str | None,
    password: str | None,
) -> None:
    """Reproduce a screen state from scratch: entry URL + recorded actions."""
    _goto(page, entry_url)
    for action in path:
        _execute_action(page, action, username, password)


def _state_matches(page: Page, node: _Node) -> bool:
    """True iff the live page IS the node's screen: exact URL plus the full
    screen_id (flags + structural digest). A mutated same-URL DOM is rejected
    so replay falls back instead of clicking wrong candidate indices."""
    if page.url != node.url:
        return False
    try:
        state = _capture_state(page, exhausted=node.exhausted)
    except RuntimeError:
        return False
    return state.screen_id == node.screen_id


def _restore(
    page: Page,
    entry_url: str,
    node: _Node,
    username: str | None,
    password: str | None,
) -> bool:
    """Return to a node's state between candidate actions.

    Tries history.back first (cheap), then a full deterministic replay.
    """
    try:
        page.go_back(wait_until="load", timeout=5000)
        _settle(page)
        _scroll_page(page)
        if _state_matches(page, node):
            return True
    except Error:
        pass
    try:
        _replay(page, entry_url, node.path, username, password)
        _scroll_page(page)
        return _state_matches(page, node)
    except (Error, RuntimeError):
        return False


# --------------------------------------------------------------------------
# Walk engine
# --------------------------------------------------------------------------


def walk(
    entry_url: str,
    role: str,
    *,
    username: str | None = None,
    password: str | None = None,
    depth: int = DEFAULT_DEPTH,
    browser: Browser | None = None,
) -> RawWalk:
    """Walk a web app breadth-first from ``entry_url`` and record observations.

    ``depth`` is the number of user-action transitions from the entry URL
    (modal opens, wizard steps, and auth round-trips each count as one hop).
    When ``browser`` is provided it is reused (and not closed); otherwise a
    headless Chromium is launched for the duration of the walk.
    """
    if browser is not None:
        return _walk(browser, entry_url, role, username, password, depth)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        try:
            return _walk(launched, entry_url, role, username, password, depth)
        finally:
            launched.close()


def _walk(
    browser: Browser,
    entry_url: str,
    role: str,
    username: str | None,
    password: str | None,
    depth: int,
) -> RawWalk:
    result = RawWalk(capability_name="", role=role)
    visited: set[str] = set()  # observation dedupe: one observation per screen_id
    expanded: set[str] = set()  # expansion dedupe: see _expansion_key
    transition_keys: set[tuple[str, str, str]] = set()
    queue: deque[_Node] = deque()

    def record_screen(state: _PageState) -> bool:
        if state.screen_id in visited:
            return False
        visited.add(state.screen_id)
        result.observations.append(_to_observation(state, role))
        return True

    def maybe_queue(state: _PageState, path: tuple[dict, ...], node_depth: int) -> None:
        if node_depth >= depth:
            return
        key = _expansion_key(state)
        if key in expanded:
            return
        expanded.add(key)
        queue.append(node_for(state, path, node_depth))

    def record_transition(from_id: str, to_id: str, trigger: str) -> None:
        key = (from_id, to_id, trigger)
        if key in transition_keys:
            return
        transition_keys.add(key)
        result.transitions.append(
            RawTransition(from_screen_id=from_id, to_screen_id=to_id, trigger=trigger, role=role)
        )

    def node_for(state: _PageState, path: tuple[dict, ...], node_depth: int) -> _Node:
        return _Node(
            screen_id=state.screen_id,
            url=state.url,
            has_dialog=state.has_dialog,
            has_overlay=state.has_blocking_overlay,
            exhausted=state.exhausted,
            path=path,
            depth=node_depth,
        )

    def handle_result(node: _Node, action: dict, trigger: str, new_state: _PageState) -> None:
        if new_state.screen_id == node.screen_id:
            return
        record_transition(node.screen_id, new_state.screen_id, trigger)
        record_screen(new_state)
        # Queue for expansion even when the screen was already observed: a
        # continuation-bearing state reached with a new query still owes us
        # its onward round-trip (see _expansion_key).
        maybe_queue(new_state, node.path + (action,), node.depth + 1)

    def expand(page: Page, node: _Node) -> None:
        state = _capture_state(page)

        # 1) Blocking consent overlay: the only meaningful action is dismissal.
        if state.consent_button is not None:
            _eval(page, _JS_OVERLAY, True)
            _settle(page)
            new_state = _capture_state(page)
            trigger = f"dismiss consent overlay via button '{state.consent_button}'"
            handle_result(node, {"kind": "dismiss"}, trigger, new_state)
            return

        # 2) Login wall with credentials: fill and submit, then continue.
        if state.has_password and password is not None:
            _eval(page, _JS_LOGIN, [username or "", password])
            _settle(page)
            new_state = _capture_state(page)
            handle_result(node, {"kind": "login"}, "submit login form", new_state)
            return

        # 3) Chooser interstitial: pick the first item in document order.
        if state.is_chooser:
            idxs = _chooser_group(state.candidates, state.copy_len) or [0]
            first = idxs[0]
            cand = state.candidates[first]
            label = cand["text"] or cand["testid"] or cand["tag"]
            _eval(page, _JS_CLICK, {"index": first, "dialog": False})
            _settle(page)
            new_state = _capture_state(page)
            trigger = f"select first option '{label}'"
            handle_result(node, {"kind": "choose_first"}, trigger, new_state)
            return

        # 4) Lazy/infinite content: scroll to the bottom, bounded by structure.
        exhausted, rounds = _scroll_page(page)
        log.debug("walker.scroll", screen=node.screen_id, exhausted=exhausted, rounds=rounds)
        if exhausted:
            new_state = _capture_state(page, exhausted=True)
            handle_result(node, {"kind": "scroll"}, "scroll feed", new_state)
            return

        # 5) Generic enumeration: candidate actions in document order. Each
        # click is re-resolved at click time by the candidate's signature
        # (with its duplicate ordinal) so in-place DOM mutations from earlier
        # candidates can never shift a bare index onto the wrong element.
        candidates = _eval(page, _JS_CANDIDATES, state.has_dialog) or []
        ordinal_at: list[int] = []
        sig_counts: dict[tuple, int] = {}
        for cand in candidates:
            sig = (cand["tag"], cand["text"], cand["href"], cand["role"],
                   cand["classes"], cand["testid"])
            ordinal_at.append(sig_counts.get(sig, 0))
            sig_counts[sig] = ordinal_at[-1] + 1
        in_place = True  # page is currently at the node's state
        for index, cand in _sample_candidates(candidates):
            if not in_place and not _restore(page, entry_url, node, username, password):
                log.warning("walker.restore_failed", screen=node.screen_id)
                return
            in_place = True
            _eval(page, "() => { window.__sidira_mark = 1; }")
            click_args = {"index": index, "dialog": state.has_dialog,
                          "sig": cand, "ordinal": ordinal_at[index]}
            clicked = _eval(page, _JS_CLICK, click_args)
            if not clicked:
                # The candidate's signature is gone from the live DOM — an
                # earlier action mutated the page. Resync before continuing.
                in_place = False
                continue
            _settle(page)
            mark_alive = bool(_eval(page, "() => window.__sidira_mark === 1", attempts=1))
            new_state = _capture_state(page)
            if new_state.screen_id == node.screen_id:
                # Same screen. If no navigation happened (toast, no-op click)
                # we can keep going in place; a reload needs a restore.
                in_place = mark_alive
                continue
            kind = "link" if cand["tag"] == "a" else "button"
            label = cand["text"] or cand["testid"] or cand["href"] or cand["tag"]
            action = {"kind": "click", "index": index, "dialog": state.has_dialog,
                      "sig": cand, "ordinal": ordinal_at[index]}
            handle_result(node, action, f"click {kind} '{label}'", new_state)
            in_place = False

    # Visit the entry screen.
    context = browser.new_context()
    try:
        page = context.new_page()
        _goto(page, entry_url)
        state = _capture_state(page)
        record_screen(state)
        maybe_queue(state, (), 0)
    finally:
        context.close()

    # BFS expansion. Each node is reproduced in a fresh context so cookie /
    # auth state is deterministic regardless of expansion order.
    while queue:
        node = queue.popleft()
        if node.depth >= depth:
            continue
        context = browser.new_context()
        try:
            page = context.new_page()
            _replay(page, entry_url, node.path, username, password)
            expand(page, node)
        finally:
            context.close()

    return result


def walk_capability(
    sandbox_base_url: str,
    capability: CapabilityConfig,
    role: str,
    user_env: str,
    password_env: str,
) -> RawWalk:
    """Walk one capability as one role. Returns the raw observation graph.

    Thin wrapper over :func:`walk`: resolves credentials from the given env
    var names (a guest role simply has no credentials set), walks every entry
    point with the capability's hop budget, and merges the results.
    """
    username = os.environ.get(user_env) if user_env else None
    password = os.environ.get(password_env) if password_env else None

    merged = RawWalk(capability_name=capability.name, role=role)
    seen_screens: set[str] = set()
    seen_transitions: set[tuple[str, str, str]] = set()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for entry in capability.entry_points:
                if "://" in entry:
                    entry_url = entry
                else:
                    entry_url = sandbox_base_url.rstrip("/") + "/" + entry.lstrip("/")
                one = walk(
                    entry_url,
                    role,
                    username=username,
                    password=password,
                    depth=capability.max_hops,
                    browser=browser,
                )
                for obs in one.observations:
                    if obs.screen_id in seen_screens:
                        continue
                    seen_screens.add(obs.screen_id)
                    merged.observations.append(obs)
                for tr in one.transitions:
                    key = (tr.from_screen_id, tr.to_screen_id, tr.trigger)
                    if key in seen_transitions:
                        continue
                    seen_transitions.add(key)
                    merged.transitions.append(tr)
        finally:
            browser.close()

    return merged
