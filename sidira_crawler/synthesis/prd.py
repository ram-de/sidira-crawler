"""Reverse-PRD synthesis.

Takes one capability's raw walks (one per role) and emits a markdown document
that reads like a PRD a PM would have written from scratch. Sections:
- User goal
- Roles
- Flow per role
- Success criteria
- Edge cases & failure modes
- Dependencies
- Implicit metric
"""

from __future__ import annotations

from sidira_crawler.llm import LLMClient
from sidira_crawler.walker import RawWalk

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
    capability_name: str,
    walks_by_role: dict[str, RawWalk],
    llm: LLMClient,
) -> str:
    """Synthesize a reverse-PRD markdown doc from per-role walks."""
    raise NotImplementedError("synthesis.prd.synthesize_reverse_prd — implement in Phase 1")
