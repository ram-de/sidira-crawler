# Sandbox acceptance gate

CI walks the pinned Streamly sandbox image with the generic walker and judges
the result against the sandbox's own ground-truth manifest. Workflow:
`.github/workflows/sandbox-walk.yml`. Harness: `scripts/sandbox_acceptance.py`.
Judge config: `acceptance/streamly-sandbox.json`.

## The two-part gate

The gate deliberately splits knowledge in two:

1. **The engine must stay generic.** `sidira_crawler/` contains zero
   customer-specific selectors, routes, copy, or test ids
   (`grep -ri streamly sidira_crawler/` is — and must remain — empty). It
   explores with generic heuristics only: consent overlays, login forms,
   chooser interstitials, lazy-feed scrolling, dialogs.
2. **The judge is allowed to know the sandbox.** The judge config carries one
   deterministic predicate matcher per manifest screen id (route equality,
   screen_id flag tokens, `url_contains`, `data-testid` presence/absence,
   visible-copy regex) plus two special transition matchers:
   - `login-roundtrip` — a "submit login form" transition out of a login
     screen where a `next=` parameter is carried through the submit (on the
     login-wall URL or forwarded to the post-login screen's URL), and the
     decoded target route is actually reached downstream of the submit in the
     transition graph.
   - `scroll-growth` — a "scroll feed" transition on a given route whose
     to-screen differs from its from-screen (scrolling changed screen state).

   There is no fuzzy scoring: a screen is matched only on real evidence.

This split is the point: the judge proves the *generic* engine discovers a
product it knows nothing about, scored against an answer key the engine never
sees. Moving any judge knowledge into the engine would invalidate the result.

## What a run does

Per run (depth 6, fresh sandbox **and** fresh browser per walk):

1. Walk each role from each of its configured `entry_paths` — one walk per
   `(role, entry path)` pair (currently guest: `/`, `/my-list`; member and
   premium: `/`, `/account/plan` — 6 walks). A secondary entry path puts
   deep guarded surfaces (the login wall behind `/my-list`, the plan/upgrade
   screens behind `/account/plan`) within the walker's depth budget without
   raising the budget for everything else.
2. Walk the determinism role (`member`) a second time from its **primary**
   entry path (`/`): both walks must observe **identical screen_id sets**
   (the sandbox's latency is seeded, the walker is deterministic — any diff
   is a regression).
3. Judge: per-role coverage vs. its threshold, all `mustDiscover` entries,
   determinism, crashes. **Any** failure exits nonzero — a missing
   `mustDiscover` entry fails the gate even if every coverage ratio passes.

### Merging semantics

A role's walks are **merged for screen coverage only**: the union of
observations by `(screen_id, url)` (and transitions by
`(from, to, trigger)`). The URL is part of the key because the same
`screen_id` can be observed at different URLs across walks — the login wall
at `/login?next=…` digests identically to the plain `/login` page, and a
`url_contains` matcher needs the variant that carries its evidence. A screen
found in any of the role's walks counts. Transition matchers
(`login-roundtrip`, `scroll-growth`, generic) are evaluated against each
**individual** walk, never the merged graph: stitching edges from different
walks would fabricate paths no single walk took — a `login-roundtrip`'s
`next=` evidence and its downstream reachability must come from one walk's
transition graph.

## raw.jsonl schema

One JSON object per line:

- Line 1 — `{"kind": "meta", "schema": 2, "image": …, "server_cmd": …,
  "run_date": …, "walker_version": …, "depth": …, "judge_config": …,
  "roles": […], "entry_paths": {role: […]}, "determinism_role": …,
  "determinism_entry_path": …}`. The date comes from `--run-date` (no hidden
  clock state).
- Then one line per observation (`"kind": "observation"` plus all
  `RawObservation` fields) and one per transition (`"kind": "transition"`
  plus all `RawTransition` fields), each tagged with `"role"`,
  `"entry_path"`, and `"walk"` (the walk label `<role>@<entry_path>`; the
  determinism re-walk is `<role>-determinism@<entry_path>`).

CI always uploads `raw.jsonl` as a workflow artifact. When the R2 secrets
(`R2_BUCKET`, `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`)
are configured it is also uploaded to
`customers/streamly-sandbox/crawl/<date>/raw.jsonl`; without them the step is
skipped with a visible notice.

## Running locally

No docker needed — point the harness at the sandbox checkout (it boots a
fresh server process per walk on an ephemeral port and waits for `/healthz`):

```bash
.venv/bin/python scripts/sandbox_acceptance.py \
  --server-cmd 'cd ~/Projects/streamly-sandbox && npm run dev' \
  --manifest ~/Projects/streamly-sandbox/ground-truth/manifest.json \
  --config acceptance/streamly-sandbox.json \
  --run-date "$(date -u +%F)" \
  --out raw.jsonl
```

The harness sets `PORT` in the child's environment (and substitutes `{port}`
if the command contains it). With `--image <ref>` it instead docker-runs a
fresh container per walk and extracts the manifest from the image via
`docker cp` (`--manifest` still overrides).

The judge functions are pure and importable; `tests/test_acceptance.py`
exercises them hermetically with synthetic walks — no sandbox, browser, or
network involved.
