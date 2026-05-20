"""Top-level orchestration — run_crawl is the single public entry point.

The flow:
1. Load customer config (capabilities, entry points, roles)
2. For each capability × role: walk via Playwright → RawWalk
3. Synthesize reverse-PRD (LLM) and reverse-Figma (structured) per capability
4. Build FeaturePack with manifest + snapshot_version
5. Diff against the prior snapshot
6. Write everything to the artifact store
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from pythia_crawler.config import load_customer_config
from pythia_crawler.llm import get_client
from pythia_crawler.schema import Capability, FeaturePack, FeaturePackManifest, ReverseFigma
from pythia_crawler.storage import get_store, write_diff
from pythia_crawler.synthesis.figma import synthesize_reverse_figma
from pythia_crawler.synthesis.prd import synthesize_reverse_prd
from pythia_crawler.walker import RawWalk, walk_capability

log = structlog.get_logger()


def run_crawl(
    customer_id: str,
    snapshot_version: str,
    config_path: str | Path | None = None,
) -> str:
    """Run a full weekly crawl for a customer. Returns the artifact URI."""
    cfg_path = Path(config_path) if config_path else _default_config_path(customer_id)
    config = load_customer_config(cfg_path)
    if config.customer_id != customer_id:
        raise ValueError(
            f"Config customer_id {config.customer_id} does not match arg {customer_id}"
        )

    log.info("crawl.start", customer_id=customer_id, snapshot=snapshot_version)
    llm = get_client()

    capabilities: list[Capability] = []
    for cap_cfg in config.capabilities:
        log.info("crawl.capability.start", capability=cap_cfg.name, roles=cap_cfg.roles)
        walks_by_role: dict[str, RawWalk] = {}
        for role in cap_cfg.roles:
            creds = _credentials_for_role(config, role)
            walks_by_role[role] = walk_capability(
                sandbox_base_url=config.sandbox_base_url,
                capability=cap_cfg,
                role=role,
                user_env=creds.env_var_user,
                password_env=creds.env_var_password,
            )

        reverse_prd = synthesize_reverse_prd(cap_cfg.name, walks_by_role, llm)
        reverse_figma: ReverseFigma = synthesize_reverse_figma(cap_cfg.name, walks_by_role)

        capabilities.append(
            Capability(
                name=cap_cfg.name,
                surfaces=list({o.url for w in walks_by_role.values() for o in w.observations}),
                roles=cap_cfg.roles,
                entry_points=cap_cfg.entry_points,
                reverse_prd=reverse_prd,
                reverse_figma=reverse_figma,
            )
        )

    pack = FeaturePack(
        manifest=FeaturePackManifest(
            customer_id=customer_id,
            snapshot_version=snapshot_version,
            generated_at=datetime.now(timezone.utc),
            capabilities=[c.name for c in capabilities],
        ),
        capabilities=capabilities,
    )

    store = get_store()
    uri = store.write_pack(pack)

    diff = _diff_against_prior(store, customer_id, snapshot_version, pack)
    write_diff(store, customer_id, snapshot_version, diff)

    log.info("crawl.done", customer_id=customer_id, snapshot=snapshot_version, uri=uri)
    return uri


def _default_config_path(customer_id: str) -> Path:
    return Path("config") / "customers" / f"{customer_id}.yaml"


def _credentials_for_role(config, role: str):
    for c in config.role_credentials:
        if c.role == role:
            return c
    raise KeyError(f"No credentials configured for role {role!r}")


def _diff_against_prior(store, customer_id: str, snapshot_version: str, pack: FeaturePack) -> dict:
    """Compute a diff against the prior snapshot. Stubbed — implement in Phase 1."""
    return {
        "customer_id": customer_id,
        "snapshot_version": snapshot_version,
        "prior_snapshot": None,
        "changes": [],
        "note": "diff implementation pending",
    }
