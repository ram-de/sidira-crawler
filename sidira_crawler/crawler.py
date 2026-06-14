"""Top-level orchestration — run_crawl is the single public entry point.

The flow:
1. Load customer config (capabilities, entry points, roles)
2. For each capability × role: walk via Playwright → RawWalk (crash-captured)
3. Honesty gate (no-crash, declared-coverage, determinism re-walk) — a failing
   crawl raises HonestyGateError and publishes NOTHING (#206 Slice D)
4. Synthesize reverse-PRD (LLM) and reverse-Figma (structured) per capability
5. Build FeaturePack with manifest + snapshot_version
6. Diff against the prior snapshot
7. Write everything to the artifact store and flip the latest pointer
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from sidira_crawler.config import CapabilityConfig, CustomerConfig, load_customer_config
from sidira_crawler.honesty import (
    DeterminismResult,
    HonestyGateError,
    HonestyVerdict,
    compare_determinism,
    evaluate_coverage,
    render_report,
)
from sidira_crawler.llm import get_client
from sidira_crawler.schema import Capability, FeaturePack, FeaturePackManifest
from sidira_crawler.storage import get_store, publish_latest, write_diff
from sidira_crawler.synthesis.cluster import CapabilityCluster
from sidira_crawler.synthesis.figma import synthesize_reverse_figma
from sidira_crawler.synthesis.graph import ScreenGraph, graph_from_walks
from sidira_crawler.synthesis.prd import synthesize_reverse_prd
from sidira_crawler.walker import RawWalk, walk_capability

log = structlog.get_logger()


def run_crawl(
    customer_id: str,
    snapshot_version: str,
    config_path: str | Path | None = None,
    config: CustomerConfig | None = None,
) -> str:
    """Run a full weekly crawl for a customer. Returns the artifact URI.

    The config can be supplied two ways: pass ``config`` directly (the dispatch
    path builds one from the simulator's ``config_json`` — see
    :mod:`sidira_crawler.dispatch`), or leave it ``None`` to load YAML from
    ``config_path`` / the default per-customer path (the cron path).

    Raises :class:`~sidira_crawler.honesty.HonestyGateError` if the crawl fails
    the honesty gate (a crash, a missed declared route, or a non-deterministic
    re-walk). On failure NOTHING is written or published — the prior snapshot's
    ``latest`` pointer is left untouched.
    """
    if config is None:
        cfg_path = Path(config_path) if config_path else _default_config_path(customer_id)
        config = load_customer_config(cfg_path)
    if config.customer_id != customer_id:
        raise ValueError(
            f"Config customer_id {config.customer_id} does not match arg {customer_id}"
        )

    log.info("crawl.start", customer_id=customer_id, snapshot=snapshot_version)

    # ── Walk phase ── walk every capability × role, capturing crashes rather
    # than letting them abort the run, so the gate can report every problem.
    walks_by_capability: dict[str, dict[str, RawWalk]] = {}
    crashes: dict[str, str] = {}
    for cap_cfg in config.capabilities:
        log.info("crawl.capability.start", capability=cap_cfg.name, roles=cap_cfg.roles)
        by_role: dict[str, RawWalk] = {}
        for role in cap_cfg.roles:
            label = f"{cap_cfg.name}/{role}"
            try:
                creds = _credentials_for_role(config, role)
                by_role[role] = walk_capability(
                    sandbox_base_url=config.sandbox_base_url,
                    capability=cap_cfg,
                    role=role,
                    user_env=creds.env_var_user,
                    password_env=creds.env_var_password,
                )
            except Exception as exc:  # noqa: BLE001 — any walk failure is a crash signal
                crashes[label] = f"{type(exc).__name__}: {exc}"
                log.error("crawl.walk.crash", target=label, error=crashes[label])
        walks_by_capability[cap_cfg.name] = by_role

    # ── Honesty gate ── evaluate before building or writing anything.
    verdict = _evaluate_honesty(config, walks_by_capability, crashes)
    if not verdict.passed:
        log.error("crawl.gate.failed", customer_id=customer_id, report=render_report(verdict))
        raise HonestyGateError(verdict)
    log.info("crawl.gate.passed", customer_id=customer_id)

    # ── Build + publish phase ── only reached on a passing verdict.
    llm = get_client()
    capabilities: list[Capability] = []
    for cap_cfg in config.capabilities:
        graph = graph_from_walks(walks_by_capability[cap_cfg.name])
        cluster = _declared_cluster(cap_cfg, graph)
        capabilities.append(
            Capability(
                name=cap_cfg.name,
                surfaces=sorted({s.route for s in graph.screens.values()}),
                roles=cap_cfg.roles,
                entry_points=cap_cfg.entry_points,
                reverse_prd=synthesize_reverse_prd(cluster, graph, llm),
                reverse_figma=synthesize_reverse_figma(cluster, graph),
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
    publish_latest(store, customer_id, snapshot_version)

    log.info("crawl.done", customer_id=customer_id, snapshot=snapshot_version, uri=uri)
    return uri


def _evaluate_honesty(
    config: CustomerConfig,
    walks_by_capability: dict[str, dict[str, RawWalk]],
    crashes: dict[str, str],
) -> HonestyVerdict:
    """Build the honesty verdict from the collected walks (#206 Slice D)."""
    coverage = []
    for cap_cfg in config.capabilities:
        observed_routes = {
            obs.route
            for walk in walks_by_capability.get(cap_cfg.name, {}).values()
            for obs in walk.observations
        }
        coverage.append(
            evaluate_coverage(cap_cfg.name, cap_cfg.entry_points, observed_routes)
        )
    determinism = _determinism_rewalk(config, walks_by_capability, crashes)
    return HonestyVerdict(coverage=coverage, determinism=determinism, crashes=crashes)


def _determinism_rewalk(
    config: CustomerConfig,
    walks_by_capability: dict[str, dict[str, RawWalk]],
    crashes: dict[str, str],
) -> DeterminismResult:
    """Re-walk one deterministic target — the first capability's first role —
    and compare its ``screen_id`` set to the original walk. Config-free: no
    ``determinism_role`` field needed for the MVP. A crash on either walk makes
    the result non-comparable, which fails the gate closed.
    """
    target_cap = next((c for c in config.capabilities if c.roles), None)
    if target_cap is None:
        return compare_determinism("<no walkable capability>", None, None)
    role = target_cap.roles[0]
    label = f"{target_cap.name}/{role}"

    first = walks_by_capability.get(target_cap.name, {}).get(role)
    if first is None:  # the original walk crashed; already recorded in `crashes`
        return compare_determinism(label, None, None)

    try:
        creds = _credentials_for_role(config, role)
        second = walk_capability(
            sandbox_base_url=config.sandbox_base_url,
            capability=target_cap,
            role=role,
            user_env=creds.env_var_user,
            password_env=creds.env_var_password,
        )
    except Exception as exc:  # noqa: BLE001 — a re-walk crash is a determinism failure
        crashes[f"{label} (determinism re-walk)"] = f"{type(exc).__name__}: {exc}"
        log.error("crawl.determinism.crash", target=label, error=str(exc))
        return compare_determinism(label, None, None)

    return compare_determinism(
        label,
        {o.screen_id for o in first.observations},
        {o.screen_id for o in second.observations},
    )


def _declared_cluster(cap_cfg: CapabilityConfig, graph: ScreenGraph) -> CapabilityCluster:
    """MVP path: the customer declared the capability, so the cluster is the
    whole per-capability walk — no LLM clustering involved."""
    screen_ids = list(graph.screens)
    entry_routes = {_path_of(url) for url in cap_cfg.entry_points}
    entry_ids = [s.screen_id for s in graph.screens.values() if s.route in entry_routes]
    return CapabilityCluster(
        name=cap_cfg.name,
        user_goal="",
        screen_ids=screen_ids,
        entry_screen_ids=entry_ids or screen_ids[:1],
    )


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path or "/"


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
