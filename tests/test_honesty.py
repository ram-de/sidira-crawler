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

import threading

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


def test_diff_failure_writes_no_pack(gate_harness, monkeypatch) -> None:
    """The diff is computed BEFORE write_pack (#22 reorder), so a diff failure
    leaves no pack at all — not even an unreachable orphan."""
    walk_results, store, published = gate_harness
    walk = RawWalk(
        capability_name="billing",
        role="admin",
        observations=[_obs("/billing", "billing::s1")],
    )
    walk_results.extend([walk, walk])

    def boom(*a, **k):
        raise RuntimeError("prior snapshot unreadable")

    monkeypatch.setattr(crawler, "_diff_against_prior", boom)

    with pytest.raises(RuntimeError, match="prior snapshot"):
        crawler.run_crawl("acme", "2026-W24")

    assert store.written == []  # diff failed before write_pack → nothing written
    assert published == []


def test_post_write_pack_failure_leaves_unreachable_orphan(gate_harness, monkeypatch) -> None:
    """A failure AFTER write_pack (here the diff write) leaves the pack on disk
    but `latest` never flipped — the orphan is provably unreachable (#22)."""
    walk_results, store, published = gate_harness
    walk = RawWalk(
        capability_name="billing",
        role="admin",
        observations=[_obs("/billing", "billing::s1")],
    )
    walk_results.extend([walk, walk])

    def boom(*a, **k):
        raise RuntimeError("diff write failed")

    monkeypatch.setattr(crawler, "write_diff", boom)

    with pytest.raises(RuntimeError, match="diff write failed"):
        crawler.run_crawl("acme", "2026-W24")

    assert len(store.written) == 1  # the pack IS written (the orphan)...
    assert published == []  # ...but `latest` never names it → unreachable


# --------------------------------------------------------------------------
# Opt-in cap×role parallelism (#28 lever 2)
# --------------------------------------------------------------------------


def test_walk_concurrency_defaults_to_serial() -> None:
    """The safe default is serial — opt-in only. Many apps enforce one session
    per user, so concurrent logins of the same account would self-invalidate."""
    assert _config().walk_concurrency == 1


def _multi_config(walk_concurrency: int) -> CustomerConfig:
    """A 3-capability × 2-role config — six walk units to spread across threads."""
    caps = [
        CapabilityConfig(
            name=name,
            entry_points=[f"https://acme.test/{name}"],
            roles=["admin", "member"],
        )
        for name in ("billing", "reports", "settings")
    ]
    return CustomerConfig(
        customer_id="acme",
        sandbox_base_url="https://acme.test",
        capabilities=caps,
        role_credentials=[
            RoleCredential(role="admin", env_var_user="U", env_var_password="P"),
            RoleCredential(role="member", env_var_user="U2", env_var_password="P2"),
        ],
        walk_concurrency=walk_concurrency,
    )


@pytest.fixture
def parallel_harness(monkeypatch):
    """Like ``gate_harness`` but with a thread-safe walk stub that records every
    (capability, role) it is asked to walk, so a test can assert the parallel
    path covers exactly the units a serial walk would — under real threads.

    ``screen_id`` is a pure function of (capability, role), so the determinism
    re-walk of the first unit reproduces an identical screen set and the gate
    passes. Returns ``(store, published, seen)``.
    """
    store = _RecordingStore()
    published: list[str] = []
    lock = threading.Lock()
    seen: list[tuple[str, str]] = []

    monkeypatch.setattr(crawler, "get_store", lambda: store)
    monkeypatch.setattr(crawler, "get_client", lambda: object())
    monkeypatch.setattr(
        crawler, "publish_latest", lambda *a, **k: published.append(a) or True
    )
    monkeypatch.setattr(crawler, "write_diff", lambda *a, **k: None)
    monkeypatch.setattr(crawler, "graph_from_walks", lambda walks: _FakeGraph())
    monkeypatch.setattr(crawler, "_declared_cluster", lambda cfg, graph: object())
    monkeypatch.setattr(crawler, "synthesize_reverse_prd", lambda *a, **k: "prd")
    monkeypatch.setattr(crawler, "synthesize_reverse_figma", lambda *a, **k: ReverseFigma())

    def fake_walk(*, capability, role, **kwargs):
        with lock:
            seen.append((capability.name, role))
        return RawWalk(
            capability_name=capability.name,
            role=role,
            observations=[_obs(f"/{capability.name}", f"{capability.name}::{role}")],
        )

    monkeypatch.setattr(crawler, "walk_capability", fake_walk)

    return store, published, seen


def test_parallel_walk_covers_every_unit_and_publishes(parallel_harness, monkeypatch) -> None:
    """With ``walk_concurrency`` > 1 every cap×role unit is still walked exactly
    once and the gate passes — thread scheduling changes order, not coverage."""
    store, published, seen = parallel_harness
    monkeypatch.setattr(crawler, "load_customer_config", lambda _p: _multi_config(4))

    uri = crawler.run_crawl("acme", "2026-W24")

    assert uri == "memory://pack/0"
    assert len(store.written) == 1
    assert len(published) == 1

    main_units = {
        (cap, role)
        for cap in ("billing", "reports", "settings")
        for role in ("admin", "member")
    }
    assert set(seen) == main_units
    # Six main units + one determinism re-walk of the first unit (billing/admin).
    assert len(seen) == len(main_units) + 1
    assert seen.count(("billing", "admin")) == 2


def test_parallel_walk_crash_is_captured_and_blocks_publish(parallel_harness, monkeypatch) -> None:
    """A crash on a worker thread is captured (not swallowed) and still fails the
    no-crash gate — crash collection is thread-safe."""
    store, published, seen = parallel_harness
    monkeypatch.setattr(crawler, "load_customer_config", lambda _p: _multi_config(4))

    fake_walk = crawler.walk_capability  # the fixture's recording stub

    def crashing(*, capability, role, **kwargs):
        if (capability.name, role) == ("reports", "member"):
            raise RuntimeError("parallel nav exploded")
        return fake_walk(capability=capability, role=role, **kwargs)

    monkeypatch.setattr(crawler, "walk_capability", crashing)

    with pytest.raises(HonestyGateError) as excinfo:
        crawler.run_crawl("acme", "2026-W24")

    assert "parallel nav exploded" in str(excinfo.value)
    assert store.written == []
    assert published == []
