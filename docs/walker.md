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

## Screen identity

A screen is **normalized route + UI-state signature**, formatted
`route::token+token`.

- **Route normalization**: path segments that look like identifiers (pure
  digits, UUIDs, long hex) collapse to `:id`; query string and fragment are
  dropped. `/title/t-101` and `/title/t-205` are the same screen.
- **State signature**: deterministic tokens derived from DOM landmarks, in
  fixed order: `overlay` (blocking consent layer present), `dialog` (open
  modal), `login` (visible password field), `chooser` (dominant
  pick-one-option group), `exhausted` (feed scrolled to its end state).

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
