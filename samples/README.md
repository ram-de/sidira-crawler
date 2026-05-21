# Samples

Example Feature Pack outputs for fictional customers. Used as:
1. Documentation of the artifact shape (what the simulator will consume)
2. Fixtures for tests that don't want to invoke a real crawl
3. Reference for `pythia-simulator` developers building the read path

| Sample | Customer | Snapshot | Capabilities |
|---|---|---|---|
| `example_acme_2026-W21/` | Acme Eats (fictional food-delivery, DoorDash-like) | 2026-W21 | reorder, checkout |

Every sample mirrors the production R2 layout exactly:

```
<sample>/
├── manifest.json                 # FeaturePackManifest
├── diff-vs-prior.json            # change summary vs prior snapshot
└── capabilities/<name>/
    ├── reverse-prd.md            # PM-shaped narrative
    └── reverse-figma.json        # structured visual/interactive spec
```

No real customer data — all content is invented for illustration.
