# sidira-crawler

Weekly product crawler that produces versioned **Feature Packs** for the Sidira simulation engine.

A Feature Pack is a structured snapshot of how a customer's product works *today* — captured in the same shape PMs use to describe new features (PRD + Figma). The Sidira simulator consumes Feature Packs alongside Persona Packs and a PM's hypothesis to validate experiments before they're built.

## What it produces

For each capability the customer cares about, the crawler emits:

- **`reverse-prd.md`** — what a PM would write if they had to document this feature from scratch (user goal, flow per role, success criteria, edge cases, dependencies)
- **`reverse-figma.json`** — the visual + interactive spec at Figma's level of abstraction (screens, components, states, copy, transitions, role-gating)

Plus a pack-level manifest, snapshot version, and a diff against the prior week.

See [`docs/feature-pack-crawler-spec.md`](docs/feature-pack-crawler-spec.md) for the full design.

## Subsystem in the Sidira architecture

```
sidira-personas/    → Persona Pack
sidira-crawler/     → Feature Pack     ← this repo
                                       ↓ both consumed by ↓
                       sidira-simulator (mirofish-inspired UI)
```

All three producers write to a shared artifact store (Cloudflare R2); the simulator reads from there. See `docs/sidira-system-architecture.md` in the personal wiki for the full picture.

## Install

```bash
pip install -e ".[dev]"
playwright install chromium
```

## Usage

### As a CLI (cron / standalone)

```bash
sidira-crawler run --customer acme --snapshot 2026-W21
```

### As a library (imported by simulator workers)

```python
from sidira_crawler import run_crawl

artifact_uri = run_crawl(customer_id="acme", snapshot_version="2026-W21")
```

## MVP scope

| What the customer supplies | What the crawler does |
|---|---|
| 5–10 priority capabilities by name | Authenticates per role, walks each entry point ~3 hops deep |
| 1–3 entry-point URLs per capability | Synthesizes reverse-PRD + reverse-Figma per capability |
| Sandbox/staging tenant + test users per role | Versions the pack, diffs against prior snapshot, writes to R2 |
| IP allowlist for the crawler's egress | |

## License

MIT
