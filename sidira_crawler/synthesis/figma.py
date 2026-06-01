"""Reverse-Figma synthesis.

Takes one capability's raw walks (one per role) and emits a structured
ReverseFigma model: screens, components, states, copy, transitions, role-gating.
"""

from __future__ import annotations

from sidira_crawler.schema import ReverseFigma
from sidira_crawler.walker import RawWalk


def synthesize_reverse_figma(
    capability_name: str,
    walks_by_role: dict[str, RawWalk],
) -> ReverseFigma:
    """Build a ReverseFigma model by merging per-role walks.

    Heuristics (deferred to implementation):
    - Screens with the same URL fingerprint are merged across roles
    - Components observed only for specific roles get visible_to_roles set
    - Transitions tagged with the role(s) that exercised them
    """
    raise NotImplementedError("synthesis.figma.synthesize_reverse_figma — implement in Phase 1")
