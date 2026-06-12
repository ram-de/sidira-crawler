"""Screen graph — the synthesis-side view of one or more raw walks.

Synthesis never re-walks anything: it consumes the raw graph the walker
recorded (either a schema-2 ``raw.jsonl`` file or in-memory ``RawWalk``
objects) merged into one deduplicated structure. Merging mirrors the
acceptance harness's coverage semantics: observations dedupe by
``screen_id`` (recording every role that saw the screen), transitions by
``(from, to, trigger)`` (recording every role that exercised them).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sidira_crawler.walker import RawWalk

#: Bounds applied when merging observations — keeps LLM prompts and
#: reverse-Figma output proportionate to what a PM would actually read.
MAX_HEADINGS = 8
MAX_COPY_SNIPPETS = 24
MAX_TESTIDS = 16


@dataclass
class ScreenNode:
    """One logical screen, merged across every walk that observed it."""

    screen_id: str
    route: str
    title: str
    roles: set[str] = field(default_factory=set)
    headings: list[str] = field(default_factory=list)
    visible_copy: list[str] = field(default_factory=list)
    data_testids: list[str] = field(default_factory=list)
    landmarks: list[str] = field(default_factory=list)
    has_dialog: bool = False
    has_blocking_overlay: bool = False


@dataclass
class ScreenEdge:
    """One observed transition, merged across every walk that exercised it."""

    from_screen_id: str
    to_screen_id: str
    trigger: str
    roles: set[str] = field(default_factory=set)


@dataclass
class ScreenGraph:
    """Deduplicated union of all walks — the synthesizer's only input."""

    screens: dict[str, ScreenNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], ScreenEdge] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)
    entry_paths: dict[str, list[str]] = field(default_factory=dict)
    run_date: str | None = None
    walker_version: str | None = None
    source: str | None = None

    def out_edges(self, screen_id: str) -> list[ScreenEdge]:
        return [e for e in self.edges.values() if e.from_screen_id == screen_id]

    def _merge_observation(self, obs: dict, role: str) -> None:
        node = self.screens.get(obs["screen_id"])
        if node is None:
            node = ScreenNode(
                screen_id=obs["screen_id"],
                route=obs["route"],
                title=obs.get("title", ""),
            )
            self.screens[node.screen_id] = node
        node.roles.add(role)
        node.has_dialog = node.has_dialog or bool(obs.get("has_dialog"))
        node.has_blocking_overlay = node.has_blocking_overlay or bool(
            obs.get("has_blocking_overlay")
        )
        _extend_bounded(node.headings, obs.get("headings", ()), MAX_HEADINGS)
        _extend_bounded(node.visible_copy, obs.get("visible_copy", ()), MAX_COPY_SNIPPETS)
        _extend_bounded(node.data_testids, obs.get("data_testids", ()), MAX_TESTIDS)
        _extend_bounded(node.landmarks, obs.get("landmarks", ()), MAX_TESTIDS)

    def _merge_transition(self, tr: dict, role: str) -> None:
        key = (tr["from_screen_id"], tr["to_screen_id"], tr["trigger"])
        edge = self.edges.get(key)
        if edge is None:
            edge = ScreenEdge(*key)
            self.edges[key] = edge
        edge.roles.add(role)


def _extend_bounded(target: list[str], items, cap: int) -> None:
    for item in items:
        if len(target) >= cap:
            return
        if item and item not in target:
            target.append(item)


def load_raw_jsonl(path: Path | str) -> ScreenGraph:
    """Load a schema-2 ``raw.jsonl`` file into a merged ScreenGraph."""
    graph = ScreenGraph(source=Path(path).name)
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            kind = obj.get("kind")
            if kind == "meta":
                schema = obj.get("schema")
                if schema != 2:
                    raise ValueError(f"Unsupported raw.jsonl schema {schema!r} (expected 2)")
                graph.roles = list(obj.get("roles", []))
                graph.entry_paths = dict(obj.get("entry_paths", {}))
                graph.run_date = obj.get("run_date")
                graph.walker_version = obj.get("walker_version")
            elif kind == "observation":
                graph._merge_observation(obj, obj["role"])
            elif kind == "transition":
                graph._merge_transition(obj, obj["role"])
            else:
                raise ValueError(f"Unknown line kind {kind!r} at {path}:{line_no}")
    if not graph.screens:
        raise ValueError(f"No observations found in {path}")
    if not graph.roles:
        graph.roles = sorted({r for s in graph.screens.values() for r in s.roles})
    return graph


def graph_from_walks(walks_by_role: dict[str, RawWalk]) -> ScreenGraph:
    """Build a ScreenGraph from in-memory walks (the config-declared MVP path)."""
    graph = ScreenGraph(roles=sorted(walks_by_role))
    for role, walk in walks_by_role.items():
        for obs in walk.observations:
            graph._merge_observation(obs.__dict__, role)
        for tr in walk.transitions:
            graph._merge_transition(tr.__dict__, role)
    return graph
