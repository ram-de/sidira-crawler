"""Domain grouping — the second level of Phase-3 clustering (capabilities → domains).

Capability clustering (``cluster.py``) groups screens into capabilities. This
module groups those capabilities one level up into DOMAINS — the navigation
hierarchy the Library needs to organize hundreds of capabilities into something
more meaningful than an alphabetized or role-filtered wall (sidira-simulator#207,
crawler #18).

Like capability names, domain labels are GENERATED, NEVER AUTHORED: synthesized
by an LLM from the capabilities' own names, goals, and surfaces — never read from
any ground-truth manifest, so the engine-purity / anti-circularity gate stays
green.

The pass is best-effort and NON-BLOCKING. Any failure — bad JSON, an unusable
LLM client, an exhausted retry budget — degrades to an empty map, leaving every
capability without a domain (the consumer renders those as "Ungrouped"). A
domain is an optional, back-compatible enrichment; it must never fail a crawl
that already passed the honesty gate.
"""

from __future__ import annotations

import json
import re

import structlog
from pydantic import BaseModel, Field

from sidira_crawler.llm import LLMClient

log = structlog.get_logger()

#: A domain label is a short human-readable phrase, e.g. "Account & billing".
#: The ceiling rejects a model that dumps a sentence (or the whole goal) as a
#: "label"; real domain names are a few words.
_MAX_DOMAIN_LABEL = 60

_SYSTEM_PROMPT = """You are a principal product manager organizing a product's
capabilities into a navigation hierarchy. You will receive the capabilities a
synthesizer already extracted from crawl data — each with a slug name, a
one-line user goal, and the routes it covers.

Group the capabilities into DOMAINS. A domain is a coherent product area that
collects several related capabilities under one human-readable label (e.g.
"Account & billing", "Content discovery", "Onboarding"). A small product may be
a single domain.

Rules:
- Use ONLY the capability slugs given. Do not invent, rename, or split them.
- Assign each capability slug to AT MOST one domain.
- Domain labels are short Title Case phrases for humans, not slugs.
- Base the grouping on the observed capabilities (names, goals, routes), not on
  outside knowledge of the product.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{
  "domains": [
    {"label": "Account & billing", "capabilities": ["account", "account_plan"]}
  ]
}"""


class CapabilityBrief(BaseModel):
    """The minimal capability descriptor the domain pass groups on."""

    name: str
    goal: str = ""
    surfaces: list[str] = Field(default_factory=list)


def synthesize_domains(
    capabilities: list[CapabilityBrief], llm: LLMClient, retries: int = 1
) -> dict[str, str]:
    """Group capabilities into domains, returning a ``{capability_name: domain}`` map.

    Best-effort and non-blocking: a single capability needs no grouping and
    returns an empty map; any LLM or validation failure after ``retries``
    degrades to an empty map. Capabilities the model leaves out simply stay
    absent from the map (the consumer renders them "Ungrouped") — only
    unparsable JSON or unknown/duplicate slugs are retried.
    """
    if len(capabilities) < 2:
        return {}
    names = {c.name for c in capabilities}
    prompt = _render_brief(capabilities)
    errors: list[str] = []
    for _attempt in range(retries + 1):
        full = prompt
        if errors:
            full += (
                "\n\nYour previous response was invalid:\n- "
                + "\n- ".join(errors)
                + "\nReturn corrected JSON only."
            )
        try:
            raw = llm.complete(_SYSTEM_PROMPT, full, max_tokens=2048)
        except Exception as exc:  # noqa: BLE001 — domain grouping is best-effort
            log.warning("synthesis.domains.llm_error", error=f"{type(exc).__name__}: {exc}")
            return {}
        mapping, errors = _parse_and_validate(raw, names)
        if not errors:
            return mapping
    log.warning("synthesis.domains.degraded", errors=errors)
    return {}


def _render_brief(capabilities: list[CapabilityBrief]) -> str:
    """Render the capability list as compact text for the grouping prompt."""
    lines = [f"CAPABILITIES ({len(capabilities)}):"]
    for cap in capabilities:
        line = f"- {cap.name}"
        if cap.goal:
            line += f" | goal: {cap.goal}"
        if cap.surfaces:
            line += f" | routes: {', '.join(cap.surfaces)}"
        lines.append(line)
    return "\n".join(lines)


def _parse_and_validate(raw: str, names: set[str]) -> tuple[dict[str, str], list[str]]:
    """Parse one domain response into a name→label map.

    Returns ``(mapping, errors)``. ``errors`` is empty when the response is
    structurally sound; a non-empty list triggers a retry. A capability the
    model omits is NOT an error — it stays out of the map and falls back to
    "Ungrouped" on the consumer.
    """
    # The whole body is guarded: this pass is best-effort, so a structurally
    # surprising response (null fields, wrong types) must degrade to a retryable
    # error, never raise out of synthesize_domains and abort a gated crawl.
    try:
        data = _extract_json(raw)
        domains = data["domains"]
        if not isinstance(domains, list):
            raise TypeError("'domains' must be a list")

        mapping: dict[str, str] = {}
        errors: list[str] = []
        for entry in domains:
            if not isinstance(entry, dict):
                errors.append(f"domain entry is not an object: {entry!r}")
                continue
            label = str(entry.get("label", "")).strip()
            if not label:
                errors.append("domain with an empty label")
                continue
            if len(label) > _MAX_DOMAIN_LABEL:
                errors.append(f"domain label too long: {label[:24]!r}...")
                continue
            caps = entry.get("capabilities", [])
            if not isinstance(caps, list):
                errors.append(f"domain {label!r}: 'capabilities' must be a list")
                continue
            for cap in caps:
                if not isinstance(cap, str):
                    errors.append(f"non-string capability {cap!r}")
                    continue
                if cap not in names:
                    errors.append(f"unknown capability {cap!r}")
                    continue
                if cap in mapping:
                    errors.append(f"capability {cap!r} assigned to multiple domains")
                    continue
                mapping[cap] = label
        return mapping, errors
    except Exception as exc:  # noqa: BLE001 — best-effort: never raise out of the pass
        return {}, [f"unparsable response: {exc}"]


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
