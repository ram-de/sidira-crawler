---
type: docs
domain: pythia
status: draft
date: 2026-05-20
---

# Feature Pack Crawler — Design Spec

**Version:** 1.0 (Draft)
**Target customer:** Lumen (enterprise)
**Status:** Brainstorm complete, awaiting review

## Overview

The Feature Pack Crawler is the third input to the Pythia simulation engine, alongside (1) the **persona pack** and (2) the **PM's hypothesis / proposed feature**. It produces a structured, weekly snapshot of how the customer's product currently works — in the same shape PMs use to describe new features (PRD + Figma) — so the simulation can compare current vs proposed apples-to-apples.

### Why this exists

A persona agent reacting to "what if we add a reorder button to the homepage?" needs to know what the homepage looks like *today*. Without a current-product representation, the simulation is ungrounded — the agent invents context, and the resulting confidence score is fiction. The crawler closes that gap automatically and on a schedule, so PMs never have to hand-author "how our product works today" before validating a hypothesis.

### Symmetry principle (load-bearing)

The PM submits a new feature as a **PRD + Figma**. The crawler produces a current-state **reverse-PRD + reverse-Figma-equivalent** per capability. The simulation compares like-shape to like-shape. This symmetry is the entire reason the artifact is shaped the way it is.

---

## Artifact: the Feature Pack

A feature pack is a versioned snapshot per customer per week. Example tag: `featurepack_acme_2026-W21`.

### Pack-level structure

| Component | Format | Purpose |
|---|---|---|
| **Manifest** | JSON | Index of all capabilities in the pack; version; crawl metadata |
| **Per-capability artifact** | Two files per capability | Reverse-PRD (markdown) + Reverse-Figma-spec (JSON) |
| **Snapshot version** | Git tag / dated folder | Reproducibility — hypotheses are evaluated against a specific snapshot |
| **Diff vs prior snapshot** | Auto-generated | Surfaces what changed since last week; sticky side-product |

### Per-capability artifact: Reverse-PRD (markdown)

What a PM would write *if they had to document this feature from scratch*. Sections:

- **User goal** — what the user is trying to accomplish
- **Roles** — which user roles experience this capability (e.g., guest, member, admin)
- **Flow per role** — entry points → steps → exit states, written as user prose
- **Success criteria** — observable signals the user succeeded
- **Edge cases & failure modes** — empty states, errors, logged-out behavior
- **Dependencies** — upstream/downstream capabilities
- **Implicit metric** — best-guess of what this feature is optimized for

### Per-capability artifact: Reverse-Figma-spec (JSON)

The visual + interactive spec at the same level of abstraction as a Figma file — but structured. Fields:

- **Screens** — frames with hierarchy, scoped per role
- **Components** — buttons, cards, inputs, modals, with semantic role
- **States per component** — default / pressed / disabled / error / empty
- **Copy** — visible text inline at component level (no separate copy library)
- **Transitions** — what click/swipe leads to what screen (Figma-prototype-arrow equivalent)
- **Annotations** — conditional visibility, data dependencies, role-gating

### Explicitly NOT in the artifact

- **Screenshots / pixel data** — PM doesn't submit screenshots for the proposed feature; symmetry says we don't capture them. (Side-store screenshots from Playwright "for free" if a future vision-capable agent needs them, but they're outside the artifact contract.)
- **Copy library as a separate file** — copy lives inline in the reverse-Figma component nodes
- **Standalone surface graph** — absorbed into reverse-Figma transitions
- **UX heuristic commentary** — the value is *this* product's specifics, not generic UX advice

---

## What a "feature" is

The unit of analysis is a **capability** — the smallest thing a single PM would write a PRD about.

| Signal | Example (yes) | Example (no — too small) | Example (no — too big) |
|---|---|---|---|
| Has a user goal | Reorder a past order | "the cart icon" | "all of notifications" |
| Has start + end state | DashPass Signup | A single button | "the whole app" |
| Spans surfaces but coherent | Search | One dropdown option | "Restaurant experience" |
| Maps to org ownership | One PM or pod owns it | No specific owner | Owned by a department |
| Ship-able independently | Yes | N/A | No — too entangled |

Hierarchy:

```
Journey         (emergent: Search → Cart → Checkout — captured via dependency links)
└── Capability  ← unit of analysis (one reverse-PRD + one reverse-Figma per capability)
    └── Component   (lives inside the reverse-Figma)
        └── State   (default / error / empty)
```

Capabilities are stable across companies (every consumer product has search/checkout/signup-equivalent). The pattern matches persona-classifier's: stable groups, variable contents.

---

## MVP scope

### What the customer supplies

| Input | Why |
|---|---|
| List of 5–10 priority capabilities by name | Eliminates the LLM capability-synthesis pass — biggest risk-reduction in v1 |
| 1–3 entry-point URLs per capability | Crawler starts from these, walks ~3 hops deep |
| Sandbox/staging tenant + test users | One test user **per role** that exists for the capability |
| IP allowlist for the crawler's egress | Standard enterprise pattern |

### What the crawler does

1. Authenticates as each declared role into the sandbox tenant
2. Walks from each capability's entry points, ~3 hops deep, recording screens / components / states / copy / transitions
3. Re-walks per role (so role-gated content is captured per role)
4. Synthesizes the reverse-PRD per capability from the role-walks
5. Emits the reverse-Figma-spec per capability
6. Tags the snapshot and diffs against the prior week

### Multi-role per capability (MVP)

Each capability declares the roles it serves. Example for DoorDash Checkout:
- `guest` — anonymous checkout
- `member` — logged-in non-DashPass
- `dashpass_member` — DashPass subscriber

The crawler walks the capability once per role using that role's test credentials. The reverse-PRD has a **Flow per role** section; the reverse-Figma-spec scopes screens by role. This roughly doubles crawl complexity for any capability with two declared roles, but matches how enterprise apps actually behave — and ungrounded single-role simulation would mislead the customer.

### Cadence

Weekly crawl. Customer can trigger ad-hoc re-crawl on demand (e.g., right after a major release).

### Infrastructure (MVP)

| Decision | Choice | Reasoning |
|---|---|---|
| Where the crawler runs | Pythia's infra | Same pattern as Userlytics / Maze / Mintlify enterprise; avoids 6–12 month per-logo deploy cycle of self-hosted |
| Crawler engine | Playwright | Cross-browser, headless, good auth + interception primitives |
| Storage | Object store (per-customer prefix) + git-tagged snapshots | Versioning is free; per-customer isolation is non-negotiable for enterprise |
| Compute | On-demand container per customer crawl | Bounded ops surface; can be scheduled |

### Storage layout

```
featurepacks/
└── <customer_id>/
    ├── manifest.json
    └── <snapshot_version>/             # e.g., 2026-W21
        ├── manifest.json
        ├── capabilities/
        │   ├── reorder/
        │   │   ├── reverse-prd.md
        │   │   └── reverse-figma.json
        │   ├── checkout/
        │   │   ├── reverse-prd.md
        │   │   └── reverse-figma.json
        │   └── ...
        └── diff-vs-prior.json
```

---

## Phase 2 scope (documented, not built)

### What gets eliminated as manual customer input

- **Capability names** — discovered by LLM synthesis, not declared
- **Entry-point URLs** — discovered by exhaustive crawl from a single seed URL
- (Sandbox credentials and role test users still required.)

### Phase 2 architecture

**Pass 1 — Exhaustive crawl** (deterministic):
Walk every reachable surface from a seed URL. Record all screens, all transitions, all copy. Output is a raw graph, no opinion about features yet.

**Pass 2 — Capability synthesis** (LLM):
Feed the raw graph to an LLM agent: *"Cluster these N screens and M transitions into capabilities. Each cluster must have a single user goal, an entry point, and a coherent boundary."* The LLM names each cluster and produces the reverse-PRD + reverse-Figma per cluster.

**Vocab alignment** (optional):
If the customer supplies an internal naming doc ("here's what we call our top 20 features"), bias Pass 2 toward that vocab so the synthesis matches how the company already talks.

### What is NEVER manual (in MVP or Phase 2)

Screens, transitions, copy, components, states, role-gated content. Always crawled, never hand-ingested. If you find yourself manually ingesting these, the crawler failed and you're back-filling — that's a bug, not a phase.

---

## How the artifact is consumed by Pythia

When a PM submits a hypothesis ("add a reorder button to the homepage for late-night munchers"):

1. Pythia parses the hypothesis → identifies affected capabilities ("Reorder", "Homepage")
2. Pulls the relevant reverse-PRDs + reverse-Figma-specs from the latest snapshot
3. Pulls the relevant personas from the customer's persona pack
4. Splices everything into the persona prompt:
   > *"You are [persona]. Here's what the app looks like today: [reverse-PRD + relevant slice of reverse-Figma]. Here's what they're proposing: [hypothesis + PM-submitted PRD + Figma]. React."*
5. Persona reacts; Pythia scores → confidence score returned to PM

The snapshot version is recorded on the simulation result. Re-running an old hypothesis re-uses its original snapshot for full reproducibility.

---

## Decisions made during brainstorm

| Decision | Rationale |
|---|---|
| Artifact shape = reverse-PRD + reverse-Figma-equivalent | Symmetry with PM input; apples-to-apples comparison |
| Skip screenshots in artifact | PM submits Figma (structural), not pixels; same on our side |
| Capability = unit of analysis | Maps to how PMs/orgs already think and own features |
| MVP: customer declares capabilities | Eliminates riskiest component (LLM synthesis quality) |
| MVP: multi-role per capability | Enterprise apps are role-gated; single-role would mislead |
| Infrastructure: Pythia's infra + customer sandbox | Avoids 6–12 month per-logo self-hosted deploy cycle |
| Engine: Playwright | Standard headless crawler with auth/interception |
| Cadence: weekly + on-demand | Weekly is the value prop; on-demand is the release-day safety net |
| Snapshot versioning is mandatory | Reproducibility of past simulations + drift detection |

---

## Open questions / risks

| Item | Notes |
|---|---|
| LLM synthesis quality (phase 2) | Need to validate clustering produces PM-recognizable capabilities. Test on DoorDash sandbox before promising it to customers. |
| Role explosion | Customer with 8 roles × 10 capabilities = 80 crawl walks. May need to constrain to "primary role + 2 variants" per capability. |
| Sandbox parity with prod | Sandbox may lack feature flags, real data shapes, or third-party integrations. Document as a known limitation; offer prod read-only crawls for high-trust customers. |
| Diff churn | UI A/B tests in the sandbox will produce spurious diffs week-to-week. Need a "ignore experimental flags" mechanism. |
| Crawl coverage gaps | Auth walls, payment steps, OTP flows can block automated crawling. May need PM-supplied scripts or browser-extension capture for those specific moments (phase 1.5). |

---

## Related

- [[pythia]] — overall project
- [[pythia-de-spec]] — pretraining corpus spec (sibling, orthogonal layer)
- `~/vaults/personal/sessions/persona-classifier/context.md` — persona pack (the other input to simulation)
- [[augur]] — consumer product (not the target customer for this crawler)
- [[lumen]] — enterprise product (target customer)
