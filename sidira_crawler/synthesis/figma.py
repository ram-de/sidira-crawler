"""Reverse-Figma synthesis.

Takes one capability cluster plus the merged screen graph and emits a
structured ReverseFigma model: screens, components, states, copy,
transitions, role-gating. This pass is fully deterministic — every component
and transition is derived from something the walker actually observed, never
from an LLM. (The LLM's only contribution to a capability is the clustering
upstream and the PRD prose downstream.)

Component derivation: a screen's interactive components are reconstructed
from the triggers of its outgoing transitions ("click button 'Play'" proves a
Play button exists), and its copy components from observed headings. A
component exercised by fewer roles than saw the screen gets
``visible_to_roles`` — observed role-gating, not inferred.
"""

from __future__ import annotations

import re

from sidira_crawler.schema import Component, ComponentState, ReverseFigma, Screen, Transition
from sidira_crawler.synthesis.cluster import CapabilityCluster
from sidira_crawler.synthesis.graph import ScreenEdge, ScreenGraph, ScreenNode

#: Cap on heading-derived copy components per screen.
MAX_HEADING_COMPONENTS = 4

#: trigger prefix -> component semantic role
_TRIGGER_ROLES = (
    ("click link ", "link"),
    ("click button ", "button"),
    ("select first option ", "select"),
    ("submit login form", "form"),
    ("scroll feed", "feed"),
)

_QUOTED_RE = re.compile(r"'(.*)'")


def synthesize_reverse_figma(cluster: CapabilityCluster, graph: ScreenGraph) -> ReverseFigma:
    """Build the ReverseFigma spec for one capability cluster."""
    member_ids = set(cluster.screen_ids)
    screens = [
        _build_screen(graph.screens[sid], graph.out_edges(sid), graph)
        for sid in cluster.screen_ids
    ]
    transitions = [
        _build_transition(edge, graph)
        for edge in graph.edges.values()
        if edge.from_screen_id in member_ids and edge.to_screen_id in member_ids
    ]
    return ReverseFigma(screens=screens, transitions=transitions)


def _build_screen(node: ScreenNode, out_edges: list[ScreenEdge], graph: ScreenGraph) -> Screen:
    components: list[Component] = []
    used_ids: set[str] = set()

    for heading in node.headings[:MAX_HEADING_COMPONENTS]:
        components.append(
            Component(
                id=_component_id("heading", heading, used_ids),
                role="heading",
                text=heading,
            )
        )

    seen_triggers: set[str] = set()
    for edge in sorted(out_edges, key=lambda e: (e.trigger, e.to_screen_id)):
        if edge.trigger in seen_triggers:
            continue
        seen_triggers.add(edge.trigger)
        role, text = _parse_trigger(edge.trigger)
        gated = sorted(edge.roles) if edge.roles != node.roles else None
        components.append(
            Component(
                id=_component_id(role, text or edge.trigger, used_ids),
                role=role,
                text=text,
                visible_to_roles=gated,
                states=[ComponentState(name="default", text=text)],
            )
        )

    role = next(iter(node.roles)) if len(node.roles) == 1 else None
    return Screen(
        id=node.screen_id,
        name=_screen_name(node),
        role=role,
        components=components,
    )


def _build_transition(edge: ScreenEdge, graph: ScreenGraph) -> Transition:
    gated = sorted(edge.roles) if edge.roles != set(graph.roles) else None
    return Transition(
        trigger=edge.trigger,
        from_screen=edge.from_screen_id,
        to_screen=edge.to_screen_id,
        role_gated=gated,
    )


def _parse_trigger(trigger: str) -> tuple[str, str | None]:
    """Map a walker trigger string to (component role, component copy)."""
    for prefix, role in _TRIGGER_ROLES:
        if trigger.startswith(prefix):
            match = _QUOTED_RE.search(trigger[len(prefix) :])
            return role, match.group(1) if match else None
    return "control", _QUOTED_RE.search(trigger).group(1) if _QUOTED_RE.search(trigger) else None


def _screen_name(node: ScreenNode) -> str:
    """A human-readable frame name: first heading, else title, else route."""
    if node.headings:
        return node.headings[0]
    if node.title:
        return node.title
    return node.route


def _component_id(role: str, text: str, used: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or role).lower()).strip("-")[:48] or role
    candidate = f"{role}-{slug}" if not slug.startswith(role) else slug
    base, n = candidate, 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate
