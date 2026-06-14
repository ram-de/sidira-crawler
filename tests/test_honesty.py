"""Tests for the honesty gate (sidira-simulator #206 Slice D).

Two layers:
1. Pure-function tests of ``sidira_crawler.honesty`` — coverage, determinism,
   the aggregate verdict, and the rendered report. No browser, no LLM, no store.
2. ``run_crawl`` integration tests that monkeypatch the walk so we can drive
   each failure mode and assert the gate's contract: a failing crawl raises
   ``HonestyGateError`` and NEVER writes a pack or flips the latest pointer; a
   clean crawl writes once and publishes once.
"""

from __future__ import annotations

import pytest

from sidira_crawler import crawler
from sidira_crawler.config import CapabilityConfig, CustomerConfig, RoleCredential
from sidira_crawler.honesty import (
    HonestyGateError,
    HonestyVerdict,
    compare_determinism,
    evaluate_coverage,
    render_report,
)
from sidira_crawler.schema import ReverseFigma
from sidira_crawler.walker import RawObservation, RawWalk


# --------------------------------------------------------------------------
# Pure-function tests
# --------------------------------------------------------------------------


def test_coverage_passes_when_every_declared_route_observed() -> None:
    cov = evaluate_coverage(
        "billing",
        ["https://app.test/billing", "https://app.test/billing/invoices?tab=open"],
        observed_routes={"/billing", "/billing/invoices", "/billing/extra"},
    )
    assert cov.passed
    assert cov.missing == []
    assert cov.declared_routes == ["/billing", "/billing/invoices"]


def test_coverage_fails_on_a_missed_declared_route() -> None:
    cov = evaluate_coverage(
        "billing",
        ["https://app.test/billing", "https://app.test/billing/invoices"],
        observed_routes={"/billing"},
    )
    assert not cov.passed
    assert cov.missing == ["/billing/invoices"]


def test_coverage_dedupes_declared_routes_by_path() -> None:
    cov = evaluate_coverage(
        "home",
        ["https://app.test/home", "https://app.test/home?ref=nav"],
        observed_routes={"/home"},
    )
    assert cov.declared_routes == ["/home"]
    assert cov.passed


def test_coverage_normalizes_declared_routes_like_the_walker() -> None:
    # The walker collapses ID-like segments and folds hash-router routes; a
    # declared entry must normalize the same way or it can never match observed.
    cov = evaluate_coverage(
        "users",
        ["https://app.test/users/123", "https://app.test/#/billing"],
        observed_routes={"/users/:id", "/#/billing"},
    )
    assert cov.declared_routes == ["/users/:id", "/#/billing"]
    assert cov.passed


def test_determinism_passes_on_identical_screen_sets() -> None:
    det = compare_determinism("billing/admin", {"a", "b"}, {"b", "a"})
    assert det.passed
    assert det.comparable
    assert det.only_first == [] and det.only_second == []


def test_determinism_fails_on_drift() -> None:
    det = compare_determinism("billing/admin", {"a", "b"}, {"a", "c"})
    assert not det.passed
    assert det.only_first == ["b"]
    assert det.only_second == ["c"]


def test_determinism_fails_closed_when_not_comparable() -> None:
    det = compare_determinism("billing/admin", None, {"a"})
    assert not det.comparable
    assert not det.passed


def test_verdict_requires_all_signals() -> None:
    good_cov = evaluate_coverage("c", ["https://x/y"], {"/y"})
    good_det = compare_determinism("c/role", {"s"}, {"s"})

    # Everything green → pass.
    assert HonestyVerdict(coverage=[good_cov], determinism=good_det, crashes={}).passed

    # A crash alone fails the verdict.
    assert not HonestyVerdict(
        coverage=[good_cov], determinism=good_det, crashes={"c/role": "Boom"}
    ).passed

    # No determinism result fails closed.
    assert not HonestyVerdict(coverage=[good_cov], determinism=None, crashes={}).passed

    # No capabilities walked fails (empty coverage is not a vacuous pass).
    assert not HonestyVerdict(coverage=[], determinism=good_det, crashes={}).passed


def test_render_report_lists_each_failure() -> None:
    verdict = HonestyVerdict(
        coverage=[evaluate_coverage("billing", ["https://x/a", "https://x/b"], {"/a"})],
        determinism=compare_determinism("billing/admin", {"s1"}, {"s2"}),
        crashes={"search/guest": "TimeoutError: nav"},
    )
    report = render_report(verdict)
    assert "FAIL" in report
    assert "/b" in report  # the missed route
    assert "DRIFT" in report
    assert "search/guest" in report


# --------------------------------------------------------------------------
# run_crawl integration — drive the gate end-to-end with a fake walk
# --------------------------------------------------------------------------


class _RecordingStore:
    """Captures whether the pack was written, standing in for the artifact store."""

    def __init__(self) -> None:
        self.written: list[object] = []

    def write_pack(self, pack: object) -> str:
        self.written.append(pack)
        return "memory://pack/0"


def _config() -> CustomerConfig:
    return CustomerConfig(
        customer_id="acme",
        sandbox_base_url="https://acme.test",
        capabilities=[
            CapabilityConfig(
                name="billing",
                entry_points=["https://acme.test/billing"],
                roles=["admin"],
            )
        ],
        role_credentials=[
            RoleCredential(role="admin", env_var_user="U", env_var_password="P")
        ],
    )


def _obs(route: str, screen_id: str) -> RawObservation:
    return RawObservation(
        url=f"https://acme.test{route}",
        route=route,
        title="t",
        role="admin",
        screen_id=screen_id,
    )


@pytest.fixture
def gate_harness(monkeypatch):
    """Wire run_crawl to an in-memory config/store and a scriptable walk.

    ``calls`` records publish_latest invocations so a test can assert nothing
    was published. ``walk_results`` is a list consumed one entry per
    walk_capability call (so the determinism re-walk gets the 2nd entry); each
    entry is either a RawWalk or an Exception to raise.
    """
    store = _RecordingStore()
    published: list[str] = []

    monkeypatch.setattr(crawler, "load_customer_config", lambda _p: _config())
    monkeypatch.setattr(crawler, "get_store", lambda: store)
    monkeypatch.setattr(crawler, "get_client", lambda: object())
    monkeypatch.setattr(
        crawler, "publish_latest", lambda *a, **k: published.append(a) or True
    )
    monkeypatch.setattr(crawler, "write_diff", lambda *a, **k: None)
    # Synthesis is exercised elsewhere; here we only care about the gate.
    monkeypatch.setattr(crawler, "graph_from_walks", lambda walks: _FakeGraph())
    monkeypatch.setattr(crawler, "_declared_cluster", lambda cfg, graph: object())
    monkeypatch.setattr(crawler, "synthesize_reverse_prd", lambda *a, **k: "prd")
    monkeypatch.setattr(crawler, "synthesize_reverse_figma", lambda *a, **k: ReverseFigma())

    walk_results: list[object] = []

    def fake_walk(**kwargs):
        result = walk_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(crawler, "walk_capability", fake_walk)

    return walk_results, store, published


class _FakeGraph:
    screens: dict = {}


def test_clean_crawl_publishes(gate_harness) -> None:
    walk_results, store, published = gate_harness
    walk = RawWalk(
        capability_name="billing",
        role="admin",
        observations=[_obs("/billing", "billing::s1")],
    )
    # First the main walk, then the identical determinism re-walk.
    walk_results.extend([walk, walk])

    uri = crawler.run_crawl("acme", "2026-W24")

    assert uri == "memory://pack/0"
    assert len(store.written) == 1
    assert len(published) == 1


def test_crash_blocks_publish(gate_harness) -> None:
    walk_results, store, published = gate_harness
    walk_results.append(RuntimeError("nav exploded"))

    with pytest.raises(HonestyGateError) as excinfo:
        crawler.run_crawl("acme", "2026-W24")

    assert "nav exploded" in str(excinfo.value)
    assert store.written == []
    assert published == []


def test_missing_declared_route_blocks_publish(gate_harness) -> None:
    walk_results, store, published = gate_harness
    # Observed a different route than the declared /billing entry point.
    walk = RawWalk(
        capability_name="billing",
        role="admin",
        observations=[_obs("/elsewhere", "elsewhere::s1")],
    )
    walk_results.extend([walk, walk])

    with pytest.raises(HonestyGateError) as excinfo:
        crawler.run_crawl("acme", "2026-W24")

    assert "/billing" in str(excinfo.value)
    assert store.written == []
    assert published == []


def test_determinism_drift_blocks_publish(gate_harness) -> None:
    walk_results, store, published = gate_harness
    first = RawWalk(
        capability_name="billing",
        role="admin",
        observations=[_obs("/billing", "billing::s1")],
    )
    second = RawWalk(
        capability_name="billing",
        role="admin",
        observations=[_obs("/billing", "billing::s2")],  # different screen_id
    )
    walk_results.extend([first, second])

    with pytest.raises(HonestyGateError) as excinfo:
        crawler.run_crawl("acme", "2026-W24")

    assert "DRIFT" in str(excinfo.value)
    assert store.written == []
    assert published == []
