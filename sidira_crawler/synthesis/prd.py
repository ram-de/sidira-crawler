"""Reverse-PRD synthesis.

Takes one capability cluster plus the merged screen graph and emits a
markdown document that reads like a PRD a PM would have written from scratch.
Sections:
- User goal
- Roles
- Flow per role
- Success criteria
- Edge cases & failure modes
- Dependencies
- Implicit metric

The LLM writes the prose; the evidence it gets is strictly what the walker
observed for this cluster's screens — per-role screen lists, copy, and the
transitions between them (including exits into other capabilities, which
inform the Dependencies section).
"""

from __future__ import annotations

from sidira_crawler.llm import LLMClient
from sidira_crawler.synthesis.cluster import CapabilityCluster
from sidira_crawler.synthesis.graph import ScreenGraph

_SYSTEM_PROMPT = """You are a product manager reverse-engineering a PRD from
observed product behavior. You will receive raw observations of a single
capability walked by one or more user roles. Produce a markdown PRD with
exactly these sections (in order):

1. User goal
2. Roles
3. Flow per role
4. Success criteria
5. Edge cases & failure modes
6. Dependencies
7. Implicit metric

Be specific. Reference real copy and screen names from the observations. Do
not invent flows you didn't see. If a section has no evidence, write
"_(no evidence observed)_" rather than fabricating."""


def synthesize_reverse_prd(
    cluster: CapabilityCluster,
    graph: ScreenGraph,
    llm: LLMClient,
) -> str:
    """Synthesize a reverse-PRD markdown doc for one capability cluster."""
    return llm.complete(_SYSTEM_PROMPT, _render_evidence(cluster, graph), max_tokens=4096)


def _render_evidence(cluster: CapabilityCluster, graph: ScreenGraph) -> str:
    member_ids = set(cluster.screen_ids)
    lines = [
        f"CAPABILITY: {cluster.name}",
        f"PROPOSED USER GOAL: {cluster.user_goal}",
        f"ENTRY SCREENS: {', '.join(cluster.entry_screen_ids)}",
        "",
        "SCREENS:",
    ]
    roles: set[str] = set()
    for sid in cluster.screen_ids:
        node = graph.screens[sid]
        roles |= node.roles
        lines.append(
            f"- {sid} | route={node.route} | seen by: {', '.join(sorted(node.roles))}"
        )
        if node.headings:
            lines.append(f"    headings: {' / '.join(node.headings[:4])}")
        if node.visible_copy:
            lines.append(f"    copy: {' | '.join(node.visible_copy[:8])}")
        if node.has_dialog:
            lines.append("    (shows a dialog)")

    lines += ["", f"ROLES OBSERVED: {', '.join(sorted(roles))}", "", "TRANSITIONS WITHIN:"]
    exits: list[str] = []
    for edge in graph.edges.values():
        if edge.from_screen_id not in member_ids:
            continue
        desc = (
            f"- {edge.from_screen_id} -> {edge.to_screen_id}"
            f" | {edge.trigger} | roles: {', '.join(sorted(edge.roles))}"
        )
        if edge.to_screen_id in member_ids:
            lines.append(desc)
        else:
            exits.append(desc)
    if exits:
        lines += ["", "EXITS INTO OTHER CAPABILITIES (use for Dependencies):", *exits]
    return "\n".join(lines)
