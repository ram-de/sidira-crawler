"""Capability clustering — the LLM pass of Phase-3 synthesis.

The walker outputs a raw graph with no opinion about features. This module
asks an LLM to cluster the graph's screens into capabilities — each with a
single user goal, an entry point, and a coherent boundary — and to name each
cluster. Screens that are pure chrome (help pages, legal, footers) may stay
unassigned.

The LLM proposes; this module validates. Every screen id in the response is
checked against the graph, names are checked against the pack-layout slug
rules, and an invalid response is retried once with the validation errors
appended so the model can correct itself.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from sidira_crawler.llm import LLMClient
from sidira_crawler.synthesis.graph import ScreenGraph

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

#: Sanity bounds on cluster count. The ceiling catches one-screen-per-
#: capability confetti; the floor is 1 because a scoped walk of a small app
#: can legitimately be a single coherent capability.
MIN_CAPABILITIES = 1
MAX_CAPABILITIES = 12

_SYSTEM_PROMPT = """You are a principal product manager reverse-engineering a
product's feature map from crawl data. You will receive every screen and
transition a generic walker observed in a web product, as structured text.

Cluster the screens into CAPABILITIES. A capability is the smallest thing a
single PM would write a PRD about: it has a single user goal, a clear entry
point, and a coherent boundary (e.g. "search", "checkout", "account
management" — not "the cart icon", not "the whole app").

Rules:
- Every capability needs: a slug name (lowercase, underscores), a one-sentence
  user goal, the screen ids that belong to it, the screen ids users enter it
  through (a subset), and your best guess at the implicit metric the feature
  is optimized for.
- Each screen id belongs to AT MOST one capability. Screens that are pure
  chrome (help, legal, marketing footers) may be left unassigned — list them
  in unassigned_screen_ids instead of forcing them into a capability.
- Use only screen ids that appear in the input. Do not invent ids.
- Base the clustering on the observed structure: routes, copy, transitions,
  and which roles see what. Do not invent capabilities you have no screens
  for.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{
  "capabilities": [
    {
      "name": "checkout",
      "user_goal": "...",
      "screen_ids": ["..."],
      "entry_screen_ids": ["..."],
      "implicit_metric": "..."
    }
  ],
  "unassigned_screen_ids": ["..."]
}"""


class CapabilityCluster(BaseModel):
    """One LLM-proposed capability: a named, goal-coherent set of screens."""

    name: str
    user_goal: str
    screen_ids: list[str] = Field(min_length=1)
    entry_screen_ids: list[str] = Field(min_length=1)
    implicit_metric: str | None = None


class ClusterResult(BaseModel):
    """The validated clustering of a whole graph."""

    capabilities: list[CapabilityCluster]
    unassigned_screen_ids: list[str] = Field(default_factory=list)


def render_graph_summary(graph: ScreenGraph) -> str:
    """Render the graph as compact structured text for the clustering prompt."""
    lines = [f"ROLES: {', '.join(graph.roles)}", "", f"SCREENS ({len(graph.screens)}):"]
    for node in graph.screens.values():
        flags = []
        if node.has_dialog:
            flags.append("dialog")
        if node.has_blocking_overlay:
            flags.append("overlay")
        lines.append(
            f"- {node.screen_id} | route={node.route}"
            f" | roles={','.join(sorted(node.roles))}"
            + (f" | flags={','.join(flags)}" if flags else "")
        )
        if node.headings:
            lines.append(f"    headings: {' / '.join(node.headings[:4])}")
        if node.visible_copy:
            lines.append(f"    copy: {' | '.join(node.visible_copy[:6])}")
    lines.append("")
    lines.append(f"TRANSITIONS ({len(graph.edges)}):")
    for edge in graph.edges.values():
        lines.append(
            f"- {edge.from_screen_id} -> {edge.to_screen_id}"
            f" | {edge.trigger} | roles={','.join(sorted(edge.roles))}"
        )
    return "\n".join(lines)


def synthesize_clusters(graph: ScreenGraph, llm: LLMClient, retries: int = 1) -> ClusterResult:
    """Cluster the graph into capabilities via the LLM, validating the response.

    An invalid response (bad JSON, unknown screen ids, slug violations…) is
    retried up to ``retries`` times with the errors appended to the prompt.
    """
    user_prompt = render_graph_summary(graph)
    errors: list[str] = []
    for _attempt in range(retries + 1):
        prompt = user_prompt
        if errors:
            prompt += (
                "\n\nYour previous response was invalid:\n- "
                + "\n- ".join(errors)
                + "\nReturn corrected JSON only."
            )
        raw = llm.complete(_SYSTEM_PROMPT, prompt, max_tokens=8192)
        result, errors = _parse_and_validate(raw, graph)
        if result is not None:
            return result
    raise ValueError("LLM clustering failed validation after retries: " + "; ".join(errors))


def _parse_and_validate(
    raw: str, graph: ScreenGraph
) -> tuple[ClusterResult | None, list[str]]:
    try:
        result = ClusterResult.model_validate(_extract_json(raw))
    except (ValueError, ValidationError) as exc:
        return None, [f"unparsable response: {exc}"]

    errors: list[str] = []
    if not MIN_CAPABILITIES <= len(result.capabilities) <= MAX_CAPABILITIES:
        errors.append(
            f"expected {MIN_CAPABILITIES}-{MAX_CAPABILITIES} capabilities,"
            f" got {len(result.capabilities)}"
        )
    known = set(graph.screens)
    seen_names: set[str] = set()
    assigned: set[str] = set()
    for cap in result.capabilities:
        if not _SLUG_RE.match(cap.name):
            errors.append(f"capability name {cap.name!r} is not a lowercase_slug")
        if cap.name in seen_names:
            errors.append(f"duplicate capability name {cap.name!r}")
        seen_names.add(cap.name)
        unknown = set(cap.screen_ids) - known
        if unknown:
            errors.append(f"{cap.name}: unknown screen ids {sorted(unknown)}")
        overlap = assigned & set(cap.screen_ids)
        if overlap:
            errors.append(f"{cap.name}: screens already assigned elsewhere {sorted(overlap)}")
        assigned |= set(cap.screen_ids)
        stray_entries = set(cap.entry_screen_ids) - set(cap.screen_ids)
        if stray_entries:
            errors.append(f"{cap.name}: entry screens not in screen_ids {sorted(stray_entries)}")
    unknown_unassigned = set(result.unassigned_screen_ids) - known
    if unknown_unassigned:
        errors.append(f"unassigned: unknown screen ids {sorted(unknown_unassigned)}")
    missing = known - assigned - set(result.unassigned_screen_ids)
    if missing:
        errors.append(
            "every screen must be assigned or explicitly unassigned; missing "
            + str(sorted(missing))
        )
    return (result, []) if not errors else (None, errors)


def _extract_json(raw: str) -> dict:
    """Pull the JSON object out of an LLM response (tolerates code fences)."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])
