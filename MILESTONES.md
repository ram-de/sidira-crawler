# pythia-crawler — Milestones

Tracks progress from scaffold through MVP delivery. Update on every significant change.

**Last updated:** 2026-05-20

## Phase tracker

| Phase | Status | Completion |
|---|---|---|
| **0. Design** | ✅ Done | 2026-05-20 |
| **1. Scaffold** | ✅ Done | 2026-05-20 |
| **2. Walker (Playwright)** | 🔲 Pending | ~1.5 weeks |
| **3. Synthesis (reverse-PRD + reverse-Figma)** | 🔲 Pending | ~1 week |
| **4. R2 wiring + diff implementation** | 🔲 Pending | ~0.5 weeks |
| **5. First customer (Lumen MVP)** | 🔲 Pending | aligned with simulator Phase 7 |

## Completed

### Phase 0 — Design (2026-05-20)
- Brainstorm with symmetry principle: artifact shape mirrors PM input (PRD + Figma)
- Capability defined as unit of analysis (with explicit yes/no/too-big/too-small criteria)
- MVP scope: customer declares capabilities + entry points + per-role test creds
- Phase 2 scope: LLM-driven capability auto-discovery (documented, deferred)
- Multi-role per capability locked in for MVP
- Infrastructure: Pythia's infra + customer sandbox creds + Playwright + Cloudflare R2
- Spec written: `docs/feature-pack-crawler-spec.md`

### Phase 1 — Scaffold (2026-05-20)
- Repo bootstrapped at `git@github.com-personal:ram-de/pythia-crawler.git`
- Package layout matches `pythia-personas` style
- `pyproject.toml` with Playwright + Pydantic + Click + boto3 + structlog + LLM SDKs
- Modules created:
  - `crawler.py` — `run_crawl()` orchestrator wired end-to-end (skeleton calls)
  - `walker.py` — Playwright walker (skeleton, `NotImplementedError`)
  - `synthesis/prd.py` + `synthesis/figma.py` — synthesis modules (skeletons)
  - `storage.py` — `LocalArtifactStore` + `R2ArtifactStore` (both working)
  - `schema.py` — Pydantic models for `FeaturePack`, `Capability`, `ReverseFigma`, etc.
  - `config.py` — YAML customer config loader (capabilities, roles, env-var creds)
  - `llm.py` — Groq primary + Anthropic fallback (mirrors `pythia-personas`)
  - `cli.py` — `pythia-crawler run --customer X --snapshot Y`
- `config/customers/example_customer.yaml` template
- Smoke tests: 5 passed, 1 deferred E2E
- Sample artifact: `samples/example_acme_2026-W21/` — fictional food-delivery customer, two capabilities (reorder, checkout), three roles (guest, member, premium_member); validates against schema
- Spec mirrored at `docs/feature-pack-crawler-spec.md`

---

## Future phases — detailed spec

### Phase 2 — Walker (Playwright)

**Goal:** A single function call walks one capability as one role and returns a structured `RawWalk` of every screen, component, and transition observed.

**Tasks:**
- Playwright session boot + per-role auth (read creds from declared env vars)
- BFS walk from each entry point, bounded by `max_hops`
- Record per screen: URL fingerprint, title, accessibility-tree outline, visible copy, component handles
- Record per transition: trigger element, from/to screen IDs, role
- Anti-bot resilience: sane user-agent, randomized timing, headless evasion baseline
- Screen-identity heuristic: URL fingerprint + accessibility-tree hash (collapses dynamic content but distinguishes layout)
- Auth flow strategy: declarative selectors per customer in YAML config (sign-in URL, username selector, password selector, success indicator) — escape hatch to LLM-driven fill in Phase 3
- Concurrency: one Playwright context per role (parallel walks across roles within a capability)
- Output: `RawWalk` dataclass per (capability, role) tuple
- Integration test against a public sandbox (TodoMVC or a self-hosted demo app)

**Acceptance:**
- `pythia-crawler run --customer example --snapshot 2026-W21` against the example config walks both capabilities × all roles to ≥80% screen coverage
- Auth failures produce a clear error before the walk starts, not silent broken pages
- A capability with 5 roles walks in ≤3× the time of a single-role walk (parallelism working)

**Dependencies:** Phase 1 ✅

**Risks:**
- **Auth wall variety** — magic-link auth, OAuth, MFA, CAPTCHA may all block walks. Document customer requirements: test users must use password auth, no MFA, no CAPTCHA in sandbox.
- **DOM volatility** — A/B-tested UI may produce different walks week-to-week; flag suspected experimental components in metadata so diff doesn't flap.
- **Headless detection** — some apps block headless Chromium. May need `playwright-stealth` plugin.

**Estimated effort:** ~1.5 weeks

---

### Phase 3 — Synthesis (reverse-PRD + reverse-Figma)

**Goal:** Convert raw walks into the artifact pair (reverse-PRD markdown + reverse-Figma JSON) the simulator consumes.

**Tasks:**
- `synthesis.figma.synthesize_reverse_figma()`:
  - Merge per-role walks into one ReverseFigma model
  - Same URL fingerprint → merged screen with `visible_to_roles` annotations on differing components
  - Component-state merging: union of states observed across roles
  - Transitions tagged with the role(s) that exercised them
- `synthesis.prd.synthesize_reverse_prd()`:
  - LLM call with all role walks + the reverse-Figma → markdown PRD
  - Strict prompt: only flows observed; mark unobserved sections as `_(no evidence observed)_`
  - Repair pass: validate against expected section structure; retry once on missing sections
- Synthesis quality assertions:
  - Each capability has ≥1 screen with ≥1 transition
  - Each role has at least one entry-point reachable
  - No invented copy: every quoted string in the PRD appears verbatim in some component
- Sample regeneration: rerun synthesis against the existing `samples/example_acme_2026-W21/` to validate the synthesis matches the hand-built sample

**Acceptance:**
- Synthesis produces an artifact pair that validates against the Pydantic schemas
- LLM-generated PRD content matches the structure of the hand-built sample (sections present, no fabrication)
- Across 10 test runs on the same raw walks, PRD output is structurally identical (LLM variance only in prose wording, not section presence or content claims)

**Dependencies:** Phase 2

**Risks:**
- **LLM hallucination of flows** — the PRD synthesis prompt must be defensive. A critic pass that flags any claim not grounded in the figma spec is worth the extra LLM call.
- **JSON parse failures on Figma synthesis** — Figma synthesis is structural (no LLM), so this risk is lower. Edge case: empty walks (no auth) — handle as empty Capability.
- **PRD section quality** — `Success criteria` and `Implicit metric` are hardest to infer; both may need PM-confirmed refinement post-launch.

**Estimated effort:** ~1 week

---

### Phase 4 — R2 wiring + diff implementation

**Goal:** Production crawler runs write to R2 with proper per-customer scoping; weekly diff surfaces what changed since last snapshot.

**Tasks:**
- `R2ArtifactStore` already implemented; verify against real R2 bucket
- Environment-based credentials (boto3 reads `R2_*` env vars; no creds in repo)
- Pointer file `customers/<id>/featurepacks/latest` → snapshot version string (so simulator doesn't list-and-sort on every read)
- `crawler._diff_against_prior()` implementation:
  - Load prior snapshot manifest
  - For each capability: load reverse-figma + reverse-prd from both snapshots
  - Structural diff: added/removed/modified screens, components, states, transitions
  - PRD diff: line-level diff with semantic similarity threshold (don't flag minor LLM rewording)
  - Emit `diff-vs-prior.json` matching the schema demonstrated in `samples/example_acme_2026-W21/diff-vs-prior.json`
- Drift detection: flag suspected A/B-test components (visible to <50% of roles, present <2 weeks)

**Acceptance:**
- A weekly crawl against a real sandbox writes a complete Feature Pack to R2 + emits a diff against last week
- The diff is human-readable; a PM can glance at it and understand what changed
- Pointer file `latest` is atomically updated only on successful crawl completion (no partial reads)

**Dependencies:** Phase 3

**Risks:**
- **Spurious diff churn from A/B tests** — must filter or down-weight experimental components
- **R2 eventual consistency** — pointer updates may race with reads. Use write-then-read-back verification.
- **Snapshot retention** — at 52 snapshots/year × 100 customers, storage grows. Decide retention policy: keep all forever, or compact older-than-N to manifest-only.

**Estimated effort:** ~0.5 weeks

---

### Phase 5 — First customer (Lumen MVP)

Aligned with `pythia-simulator` Phase 7. See that repo's `MILESTONES.md` for full spec.

Crawler-specific requirements for first customer:
- Sandbox creds exchange + IP allowlist
- Capability list confirmed with the customer (5–10 priority capabilities)
- Entry-point URLs validated (sandbox must serve them)
- Per-role test users confirmed (one per declared role)
- Weekly schedule slot chosen (typically off-peak in customer's region)
- First crawl runbook + failure escalation procedure

**Acceptance:**
- Customer's first Feature Pack lands successfully
- Subsequent weekly crawls run unattended for ≥4 consecutive weeks
- Diff reports are reviewed by the customer's PMs at least monthly

**Dependencies:** Phases 1–4 + simulator Phase 6

## Decisions log
See `docs/feature-pack-crawler-spec.md` § "Decisions made during brainstorm" for full design choices.

System-level architecture: `docs/pythia-system-architecture.md` (in personal wiki for now; will move into `pythia-simulator` repo when that's scaffolded).

## Related artifacts
- Persona-pack producer: [`pythia-personas`](https://github.com/ram-de/pythia-personas)
- Personal wiki entry: `~/vaults/personal/wiki/projects/pythia.md`
- Active working notes: `~/vaults/personal/sessions/persona-classifier/context.md`
