"""Playwright walker — authenticates per role, walks from each entry point, records observations.

The walker produces a raw graph (screens, components, transitions) per role per
capability. Synthesis modules turn that graph into reverse-PRD + reverse-Figma.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pythia_crawler.config import CapabilityConfig


@dataclass
class RawObservation:
    """One observed screen during a walk."""

    url: str
    title: str
    role: str
    screen_id: str
    dom_outline: str
    visible_copy: list[str] = field(default_factory=list)
    component_handles: list[dict] = field(default_factory=list)


@dataclass
class RawTransition:
    """One observed transition between screens."""

    from_screen_id: str
    to_screen_id: str
    trigger: str
    role: str


@dataclass
class RawWalk:
    """Output of a single role's walk through a single capability."""

    capability_name: str
    role: str
    observations: list[RawObservation] = field(default_factory=list)
    transitions: list[RawTransition] = field(default_factory=list)


def walk_capability(
    sandbox_base_url: str,
    capability: CapabilityConfig,
    role: str,
    user_env: str,
    password_env: str,
) -> RawWalk:
    """Walk one capability as one role. Returns the raw observation graph.

    Implementation deferred — this is the heaviest module and gets its own
    implementation plan.
    """
    raise NotImplementedError("walker.walk_capability — implement in Phase 1 tasks")
