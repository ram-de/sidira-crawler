"""Pack assembly — raw graph in, schema-valid FeaturePack out.

The Phase-3 pipeline:
1. Cluster the graph's screens into capabilities (LLM, validated)
2. Per capability: reverse-Figma (deterministic) + reverse-PRD (LLM)
3. Assemble the FeaturePack with manifest + crawl metadata

Pydantic validates the whole pack on construction; a pack that reaches the
artifact store is schema-valid by definition.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import structlog

from sidira_crawler.llm import LLMClient
from sidira_crawler.schema import Capability, FeaturePack, FeaturePackManifest
from sidira_crawler.synthesis.cluster import (
    CapabilityCluster,
    ClusterResult,
    synthesize_clusters,
)
from sidira_crawler.synthesis.figma import synthesize_reverse_figma
from sidira_crawler.synthesis.graph import ScreenGraph
from sidira_crawler.synthesis.prd import synthesize_reverse_prd

log = structlog.get_logger()


def synthesize_pack(
    graph: ScreenGraph,
    llm: LLMClient,
    customer_id: str,
    snapshot_version: str,
    clusters: ClusterResult | None = None,
) -> FeaturePack:
    """Run the full Phase-3 synthesis over a merged screen graph.

    ``clusters`` may be supplied (the config-declared MVP path builds them
    from the customer config); when omitted they are discovered by the LLM.
    """
    if clusters is None:
        log.info("synthesis.cluster.start", screens=len(graph.screens), edges=len(graph.edges))
        clusters = synthesize_clusters(graph, llm)
    log.info(
        "synthesis.clustered",
        capabilities=[c.name for c in clusters.capabilities],
        unassigned=len(clusters.unassigned_screen_ids),
    )

    capabilities = [
        _synthesize_capability(cluster, graph, llm) for cluster in clusters.capabilities
    ]

    manifest = FeaturePackManifest(
        customer_id=customer_id,
        snapshot_version=snapshot_version,
        generated_at=datetime.now(timezone.utc),
        capabilities=[c.name for c in capabilities],
        crawl_metadata=_crawl_metadata(graph, clusters),
    )
    return FeaturePack(manifest=manifest, capabilities=capabilities)


def _synthesize_capability(
    cluster: CapabilityCluster, graph: ScreenGraph, llm: LLMClient
) -> Capability:
    log.info("synthesis.capability.start", capability=cluster.name, screens=len(cluster.screen_ids))
    reverse_figma = synthesize_reverse_figma(cluster, graph)
    reverse_prd = synthesize_reverse_prd(cluster, graph, llm)
    roles = sorted({r for sid in cluster.screen_ids for r in graph.screens[sid].roles})
    return Capability(
        name=cluster.name,
        surfaces=sorted({graph.screens[sid].route for sid in cluster.screen_ids}),
        roles=roles,
        entry_points=sorted({graph.screens[sid].route for sid in cluster.entry_screen_ids}),
        reverse_prd=reverse_prd,
        reverse_figma=reverse_figma,
        implicit_metric=cluster.implicit_metric,
    )


def _crawl_metadata(graph: ScreenGraph, clusters: ClusterResult) -> dict[str, str]:
    meta = {
        "screens": str(len(graph.screens)),
        "transitions": str(len(graph.edges)),
        "unassigned_screens": str(len(clusters.unassigned_screen_ids)),
        "roles": ",".join(graph.roles),
    }
    if graph.source:
        meta["source"] = graph.source
    if graph.run_date:
        meta["run_date"] = graph.run_date
    if graph.walker_version:
        meta["walker_version"] = graph.walker_version
    return meta


def weekly_bucket(run_date: str) -> str:
    """ISO-week snapshot version for a YYYY-MM-DD run date, e.g. ``2026-W24``."""
    year, week, _ = date.fromisoformat(run_date).isocalendar()
    return f"{year}-W{week:02d}"
