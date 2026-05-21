# pythia-crawler — Milestones

Tracks progress from scaffold through MVP delivery. Update on every significant change.

**Last updated:** 2026-05-20

## Phase tracker

| Phase | Status | Completion |
|---|---|---|
| **0. Design** | ✅ Done | 2026-05-20 |
| **1. Scaffold** | ✅ Done | 2026-05-20 |
| **2. Walker (Playwright)** | 🔲 Pending | — |
| **3. Synthesis (reverse-PRD + reverse-Figma)** | 🔲 Pending | — |
| **4. R2 wiring + diff implementation** | 🔲 Pending | — |
| **5. First customer (Lumen MVP)** | 🔲 Pending | — |

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

## Next up (Phase 2 — Walker)

Implement `walker.walk_capability(...)`:
1. Playwright session boot + per-role auth (read creds from declared env vars)
2. BFS walk from each entry point, bounded by `max_hops`
3. Record per screen: URL, title, accessibility-tree outline, visible copy, component handles
4. Record per transition: trigger element, from/to screen IDs, role
5. Anti-bot resilience: sane user-agent, randomized timing, headless evasion baseline
6. Output: `RawWalk` dataclass per (capability, role) tuple

Open questions to resolve before starting:
- What's the screen-identity heuristic? URL fingerprint + DOM hash? URL only?
- How do we detect role-gated content vs random content? Snapshot diffs across roles on same URL?
- Auth flows: declarative selectors per customer, or LLM-driven fill?

## Decisions log
See `docs/feature-pack-crawler-spec.md` § "Decisions made during brainstorm" for full design choices.

System-level architecture: `docs/pythia-system-architecture.md` (in personal wiki for now; will move into `pythia-simulator` repo when that's scaffolded).

## Related artifacts
- Persona-pack producer: [`pythia-personas`](https://github.com/ram-de/pythia-personas)
- Personal wiki entry: `~/vaults/personal/wiki/projects/pythia.md`
- Active working notes: `~/vaults/personal/sessions/persona-classifier/context.md`
