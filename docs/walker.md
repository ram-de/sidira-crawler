# Walker

The walker (`sidira_crawler/walker.py`) is the first stage of the crawler
pipeline: it drives a real headless Chromium session through a product and
emits a `RawWalk` — the set of screens it observed and the user actions that
connect them. Everything downstream (synthesis, diffing, packs) consumes this
shape.

## Inputs

| Input | Meaning |
|---|---|
| `entry_url` | Where the walk starts. One walk explores outward from one entry point. |
| `role` + optional `username` / `password` | Credentials used if a login wall appears. With no credentials the walker still *observes* the login screen but cannot pass it. |
| `depth` (default 6) | Budget in **user-action hops** from the entry point. |

CLI:

```
sidira-crawler walk --url URL --role ROLE [--username U] [--password P] [--depth 6] [--out raw_walk.json]
```

`walk_capability(...)` is a thin wrapper used by the weekly crawl: it resolves
credentials from the env-var names in the customer config (guest roles have
none), walks each of the capability's entry points with `depth =
capability.max_hops`, and merges the results into one `RawWalk`.

## Depth semantics

Depth counts *user actions*, not page loads. Each of these is one hop:

- clicking a link or button (including one that opens a modal),
- dismissing a consent overlay,
- submitting a login form,
- picking an option on a chooser interstitial,
- scrolling a feed to exhaustion.

A login round-trip therefore costs what it costs the user: guarded page →
login screen is the navigation hop, submitting the form is another, the
chooser after it another. The default of 6 sits comfortably above the longest
flow the walker must capture today (4 hops), so a missed screen indicates a
walker failure, never budget exhaustion.

## Full-crawl config guidance — wide-and-shallow (#28)

`max_hops` is a *budget*, not a target: a shallow app naturally exhausts its
actions well below it. But for a **full multi-capability crawl** the budget you
configure matters, and profiling against the Streamly sandbox (sidira-crawler#28)
gives a sharp recommendation:

**Configure all capabilities, both roles, `max_hops: 2–3` — not `≥4`.**

Two independent reasons, and deep depth loses on both:

- **Slow.** Every node is rebuilt from the entry URL by full replay (a fresh
  browser context per node, for pristine session isolation), so each replayed
  step re-pays scroll + settle. The cost is `O(nodes × path-length)` and is
  negligible at shallow depth but explodes deep: a depth-5 single-capability
  walk took ~380s, of which ~95% was conservative idle-waiting; a wide
  6-capability × 2-role crawl at depth 3 walked + gated + published in ~6.5 min.
- **Gate-rejected.** Deep walks of a stateful SPA are *non-deterministic* —
  three depth-5 runs of the same walk produced 23 / 24 / 25 screens, the
  variance concentrated entirely in session-state artifacts (my-list contents,
  accumulated overlay/dialog states). The honesty gate's zero-tolerance
  determinism re-walk rejects them regardless of speed, so a deep crawl is also
  *unpublishable*.

Deeper budgets remain valid for a **targeted single-capability** walk where you
knowingly accept the speed and the determinism risk; they are simply not the
shape of a routine full crawl.

### Opt-in walk parallelism (`walk_concurrency`)

`run_crawl` walks capability × role serially. Each walk is fully isolated (its
own browser + fresh login), so setting `walk_concurrency: N` on the customer
config fans the cap×role units across `N` threads and cuts wide-crawl
wall-clock. It is **opt-in, default `1`**: many apps enforce
single-session-per-user, so N concurrent logins of the same role account would
invalidate each other's cookies mid-walk and crash the no-crash gate. Raise it
only for customers whose test accounts tolerate concurrent sessions. The
determinism re-walk always runs serially.

## Screen identity

A screen is **normalized route + UI-state signature**, formatted
`route::token+token+s1a2b3c`.

- **Route normalization**: path segments that look like identifiers (pure
  digits, UUIDs, long hex) collapse to `:id`; query string and plain
  fragments are dropped. `/title/t-101` and `/title/t-205` are the same
  screen. Hash **routes** (`#/...`, `#!/...`) are real navigation in
  hash-router SPAs: their fragment path is folded into the route with the
  same `:id` normalization (`/app#/settings`), while plain `#anchor`
  fragments are never navigation candidates.
- **State signature**: deterministic flag tokens derived from DOM landmarks,
  in fixed order: `overlay` (blocking consent layer present), `dialog` (open
  modal), `login` (visible password field), `chooser` (dominant
  pick-one-option group), `exhausted` (feed scrolled to its end state) —
  followed by a **structural digest** token (`s` + first 6 hex of sha1) so
  same-route structural states with none of those flags (wizard steps, tab
  panels, drawers) are still distinct screens. The digest hashes structure,
  never text content: the sorted set of `data-testid` values with digit runs
  stripped, the sorted set of ARIA landmark roles, the sorted multiset of
  visible form-control types, and a bucketed heading count (0 / 1 / 2 / 3-5 /
  6+). Same template + different content (entity pages, repeated feed cards)
  collapses; lazy-load growth of repeated cards usually stays in bucket, and
  a bucket crossing is bounded noise rather than a missed state.

Two walks of the same build produce identical screen ids; the BFS enumerates
candidates in document order, so transition sets are deterministic too.
Heading *text* is recorded on the observation but deliberately excluded from
the signature — content varies per entity, structure does not.

## Hostile-trait guarantees

| Trait | Guaranteed outcome |
|---|---|
| JS-only SPA (empty HTML shell) | Settle = load → network-idle → DOM-stable polling; the rendered app is what gets captured. |
| Blocking consent overlay | The pre-dismissal state is its own screen; dismissal (affirmative-verb button: accept / agree / allow / got it / ok / continue …) is a recorded one-hop transition to the clean screen. |
| Login wall with `?next=` | Guard → login is a transition; with credentials, submit → (interstitials) → original target are all recorded hops. Without credentials the login screen is still observed. Continuation-bearing screens (login walls, chooser interstitials) are *observed* once but *expanded* once per distinct URL, so two guarded targets bouncing to the same login screen with different `?next=` continuations are both round-tripped. |
| Chooser interstitial | Recognized as its own screen; the walker picks the **first** option deterministically and records the hop. |
| Lazy-loaded content | Incremental scroll during settle so below-the-fold content is captured. |
| Bounded infinite feed | Scrolling stops by *structure* (3 consecutive no-growth stalls or stable height), never by the hard cap (30 rounds). If the trailing in-flow element changed since first paint, the exhausted state is recorded as its own screen. |
| Modal that changes the URL | A new screen, one hop in, one hop (close/back) out. |
| Toasts / `aria-live` regions | Never screens. Live-region subtrees are excluded from capture and signatures, so a transient toast cannot fork screen identity. |

## Output shape

`RawWalk` (JSON via the CLI) is:

```
{
  "capability_name": str,
  "role": str,
  "observations": [RawObservation, ...],
  "transitions":  [RawTransition, ...]
}
```

`RawObservation`: `url`, `route`, `title`, `role`, `screen_id`, `headings`,
`dom_outline`, `visible_copy`, `data_testids` (sorted), `landmarks`,
`has_dialog`, `has_blocking_overlay`. All list/text fields are bounded.

`RawTransition`: `from_screen_id`, `to_screen_id`, `trigger` (human-readable
action, e.g. `click "Recipe feed"`, `submit login form`, `scroll feed`),
`role`.

## Anti-circularity rule

The walker engine must stay **target-agnostic**: no customer-specific
selectors, routes, copy strings, or test ids may appear in `sidira_crawler/`.
Anything the walker needs to handle must be expressed as a generic heuristic
(structural landmarks, verb classes, growth detection). Per-customer
knowledge lives in `config/customers/*.yaml` only. Test fixtures under
`tests/fixture_app/` are exempt — they are the hostile playground, not the
engine.
