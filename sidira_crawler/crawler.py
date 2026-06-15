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

from concurrent.futures import ThreadPoolExecutor
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
from sidira_crawler.synthesis.domains import CapabilityBrief, synthesize_domains
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
    # Each walk is fully isolated (own browser + fresh login), so when the
    # customer opts in (``walk_concurrency`` > 1, #28 lever 2) the cap×role units
    # run on a bounded thread pool; otherwise they run serially (the default).
    walks_by_capability: dict[str, dict[str, RawWalk]] = {
        cap_cfg.name: {} for cap_cfg in config.capabilities
    }
    crashes: dict[str, str] = {}
    units = [
        (cap_cfg, role) for cap_cfg in config.capabilities for role in cap_cfg.roles
    ]
    concurrency = max(1, config.walk_concurrency)
    if concurrency == 1:
        results = [_walk_unit(config, cap_cfg, role) for cap_cfg, role in units]
    else:
        log.info("crawl.parallel.start", workers=concurrency, units=len(units))
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(
                pool.map(lambda u: _walk_unit(config, u[0], u[1]), units)
            )
    for cap_name, role, walk, crash in results:
        label = f"{cap_name}/{role}"
        if crash is not None:
            crashes[label] = crash
            log.error("crawl.walk.crash", target=label, error=crash)
        else:
            walks_by_capability[cap_name][role] = walk

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

    # Second-level clustering: group capabilities into domains (#18). Best-effort
    # and non-blocking — a failure (or the bare LLM stub used by the gate tests)
    # leaves every capability ungrouped rather than failing a gated crawl.
    domain_map = synthesize_domains(
        [CapabilityBrief(name=c.name, surfaces=c.surfaces) for c in capabilities], llm
    )
    if domain_map:
        capabilities = [
            c.model_copy(update={"domain": domain_map.get(c.name)}) for c in capabilities
        ]

    pack = FeaturePack(
        manifest=FeaturePackManifest(
            customer_id=customer_id,
            snapshot_version=snapshot_version,
            generated_at=datetime.now(timezone.utc),
            capabilities=[c.name for c in capabilities],
            capability_domains={c.name: c.domain for c in capabilities if c.domain},
        ),
        capabilities=capabilities,
    )

    store = get_store()

    # ── Atomicity (#22) ── `publish_latest` is the SOLE visibility-making write and
    # runs LAST: the simulator only ever resolves the `latest` pointer. Compute the
    # diff (the one step that can read the prior snapshot and fail) BEFORE
    # `write_pack`, so a diff failure writes no pack at all. After `write_pack` the
    # only ops are the diff write and the pointer flip; a failure in either leaves an
    # orphan snapshot that `latest` never names — provably unreachable, so "a crawl
    # that doesn't finish publishes nothing" still holds. (Eliminating the orphan
    # outright — stage→promote, or cleanup-on-delete — needs a store move/delete
    # primitive; deferred, see issue #22.)
    diff = _diff_against_prior(store, customer_id, snapshot_version, pack)
    uri = store.write_pack(pack)
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


def _walk_unit(
    config: CustomerConfig, cap_cfg: CapabilityConfig, role: str
) -> tuple[str, str, RawWalk | None, str | None]:
    """Walk one capability×role unit, capturing any failure as a crash signal.

    Returns ``(capability_name, role, walk_or_None, crash_msg_or_None)``. Pure
    and side-effect-free apart from the walk itself, so it is safe to run on a
    worker thread when ``walk_concurrency`` > 1 — each call builds its own
    Playwright instance + browser inside :func:`walk_capability`, and the
    walker's only module-level mutable state (the page-keyed pending-request map)
    is thread-local, so siblings share nothing.
    """
    try:
        creds = _credentials_for_role(config, role)
        walk = walk_capability(
            sandbox_base_url=config.sandbox_base_url,
            capability=cap_cfg,
            role=role,
            user_env=creds.env_var_user,
            password_env=creds.env_var_password,
        )
        return cap_cfg.name, role, walk, None
    except Exception as exc:  # noqa: BLE001 — any walk failure is a crash signal
        return cap_cfg.name, role, None, f"{type(exc).__name__}: {exc}"


def _diff_against_prior(store, customer_id: str, snapshot_version: str, pack: FeaturePack) -> dict:
    """Compute a diff against the prior snapshot. Stubbed — implement in Phase 1."""
    return {
        "customer_id": customer_id,
        "snapshot_version": snapshot_version,
        "prior_snapshot": None,
        "changes": [],
        "note": "diff implementation pending",
    }
