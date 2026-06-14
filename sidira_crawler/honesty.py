"""Honesty gate for the real ``run_crawl`` path (sidira-simulator #206 Slice D).

A real customer app has no ground-truth manifest, so the mock acceptance gate
(``scripts/sandbox_acceptance.py``: ``mustDiscover`` + per-role coverage ratio)
does not apply. This module keeps the three ground-truth-free signals, all of
them hard fails — a crawl that trips any one publishes nothing:

1. **No-crash** — every ``(capability, role)`` walk completed without raising.
2. **Declared-coverage** — every declared ``entry_point`` route produced at
   least one observed screen.
3. **Determinism re-walk** — re-walking one target yields an identical
   ``screen_id`` set (zero tolerance, mirroring the sandbox gate).

The verdict is pure data; :func:`sidira_crawler.crawler.run_crawl` evaluates it
*before* writing or publishing anything, so a failing crawl never flips the
``featurepacks/latest`` pointer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class CoverageResult:
    """Declared-coverage outcome for a single capability."""

    capability: str
    declared_routes: list[str]
    observed_routes: list[str]
    missing: list[str]

    @property
    def passed(self) -> bool:
        return not self.missing


@dataclass
class DeterminismResult:
    """Outcome of the determinism re-walk for the chosen target."""

    target: str
    only_first: list[str]
    only_second: list[str]
    comparable: bool

    @property
    def passed(self) -> bool:
        # A non-comparable re-walk (crash / no target) fails closed.
        return self.comparable and not self.only_first and not self.only_second


@dataclass
class HonestyVerdict:
    """Aggregate verdict over all three signals."""

    coverage: list[CoverageResult] = field(default_factory=list)
    determinism: DeterminismResult | None = None
    crashes: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (
            bool(self.coverage)
            and all(c.passed for c in self.coverage)
            and self.determinism is not None
            and self.determinism.passed
            and not self.crashes
        )


class HonestyGateError(RuntimeError):
    """Raised when a crawl fails the honesty gate. Carries the full verdict;
    its message is the rendered report. The caller must not publish."""

    def __init__(self, verdict: HonestyVerdict) -> None:
        self.verdict = verdict
        super().__init__(render_report(verdict))


def _route_of(entry: str) -> str:
    """Normalize a declared entry point the SAME way the walker normalizes
    observed routes, so the two compare equal. This must reuse
    ``walker._normalize_route`` — a plain ``urlsplit().path`` would miss two
    cases the walker handles: ID-like segments (declared ``/users/123`` vs
    observed ``/users/:id``) and hash-router routes (``/#/billing``, whose
    fragment a plain path-split silently drops). Getting this wrong fails clean
    crawls at the coverage check."""
    from sidira_crawler.walker import _normalize_route

    return _normalize_route(entry)


def evaluate_coverage(
    capability: str,
    declared_entry_points: Iterable[str],
    observed_routes: Iterable[str],
) -> CoverageResult:
    """A declared entry point is covered when its route was observed at least
    once. A miss (404, dead link, auth wall that yields no screen) fails."""
    declared = list(dict.fromkeys(_route_of(e) for e in declared_entry_points))
    observed = set(observed_routes)
    missing = [r for r in declared if r not in observed]
    return CoverageResult(
        capability=capability,
        declared_routes=declared,
        observed_routes=sorted(observed),
        missing=missing,
    )


def compare_determinism(
    target: str,
    first_screen_ids: Iterable[str] | None,
    second_screen_ids: Iterable[str] | None,
) -> DeterminismResult:
    """Two walks of the same target must observe an identical ``screen_id`` set.
    A missing walk (the target crashed) is not comparable and fails closed."""
    if first_screen_ids is None or second_screen_ids is None:
        return DeterminismResult(target=target, only_first=[], only_second=[], comparable=False)
    a = set(first_screen_ids)
    b = set(second_screen_ids)
    return DeterminismResult(
        target=target,
        only_first=sorted(a - b),
        only_second=sorted(b - a),
        comparable=True,
    )


def render_report(verdict: HonestyVerdict) -> str:
    """A human-readable summary, mirroring the sandbox gate's report style."""
    lines: list[str] = []
    status = "PASS" if verdict.passed else "FAIL"
    lines.append(f"honesty gate: {status}")

    lines.append("declared coverage:")
    if not verdict.coverage:
        lines.append("  (no capabilities walked)")
    for cov in verdict.coverage:
        if cov.passed:
            lines.append(f"  {cov.capability}: all {len(cov.declared_routes)} declared route(s) reached")
        else:
            lines.append(f"  {cov.capability}: MISSING {cov.missing}")

    det = verdict.determinism
    if det is None:
        lines.append("determinism: not run")
    elif not det.comparable:
        lines.append(f"determinism ({det.target}): NOT COMPARABLE (target did not walk twice)")
    elif det.passed:
        lines.append(f"determinism ({det.target}): identical screen set")
    else:
        lines.append(f"determinism ({det.target}): DRIFT")
        if det.only_first:
            lines.append(f"  only in first walk:  {det.only_first}")
        if det.only_second:
            lines.append(f"  only in second walk: {det.only_second}")

    if verdict.crashes:
        lines.append("crashes:")
        for target, err in sorted(verdict.crashes.items()):
            lines.append(f"  {target}: {err}")
    else:
        lines.append("crashes: none")

    return "\n".join(lines)
