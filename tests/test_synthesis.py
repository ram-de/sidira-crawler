"""Hermetic tests for Phase-3 synthesis (graph -> clusters -> pack).

No network, no LLM, no browser: the LLM is a canned fake, the raw walk is a
synthetic invented product ("brewlog", a coffee-journal app — deliberately
not the sandbox), and packs are written to tmp_path local stores.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from sidira_crawler.schema import FeaturePack, FeaturePackManifest, ReverseFigma
from sidira_crawler.storage import LocalArtifactStore, publish_latest
from sidira_crawler.synthesis.cluster import (
    CapabilityCluster,
    synthesize_clusters,
)
from sidira_crawler.synthesis.figma import synthesize_reverse_figma
from sidira_crawler.synthesis.graph import load_raw_jsonl
from sidira_crawler.synthesis.pack import synthesize_pack, weekly_bucket
from sidira_crawler.synthesis.prd import synthesize_reverse_prd

_COMPARE = Path(__file__).resolve().parent.parent / "scripts" / "pack_structure_compare.py"
_spec = importlib.util.spec_from_file_location("pack_structure_compare", _COMPARE)
psc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(psc)


# --------------------------------------------------------------------------
# Synthetic raw.jsonl builders (invented coffee-journal app)
# --------------------------------------------------------------------------

META = {
    "kind": "meta",
    "schema": 2,
    "run_date": "2026-06-11",
    "walker_version": "0.1.0",
    "roles": ["guest", "member"],
    "entry_paths": {"guest": ["/"], "member": ["/"]},
}


def obs(screen_id, role, route=None, **extra):
    base = {
        "kind": "observation",
        "walk": f"{role}@/",
        "role": role,
        "entry_path": "/",
        "url": f"http://x{route or screen_id.split('::')[0]}",
        "route": route or screen_id.split("::")[0],
        "title": "Brewlog",
        "screen_id": screen_id,
        "headings": [],
        "dom_outline": "",
        "visible_copy": [],
        "data_testids": [],
        "landmarks": [],
        "has_dialog": False,
        "has_blocking_overlay": False,
    }
    base.update(extra)
    return base


def tr(from_id, to_id, trigger, role):
    return {
        "kind": "transition",
        "walk": f"{role}@/",
        "role": role,
        "entry_path": "/",
        "from_screen_id": from_id,
        "to_screen_id": to_id,
        "trigger": trigger,
    }


HOME, LOG, LOGIN, HELP = "/::s1", "/log::s2", "/login::login+s3", "/help::s4"

LINES = [
    META,
    obs(HOME, "guest", headings=["Your brews, remembered"], visible_copy=["Start logging"]),
    obs(HOME, "member", headings=["Your brews, remembered", "Recent brews"]),
    obs(LOG, "member", headings=["New brew entry"], visible_copy=["Save brew"]),
    obs(LOGIN, "guest", headings=["Sign in to Brewlog"]),
    obs(HELP, "guest", headings=["Help center"]),
    obs(HELP, "member", headings=["Help center"]),
    tr(HOME, LOGIN, "click link 'Sign In'", "guest"),
    tr(HOME, LOG, "click button 'New entry'", "member"),
    tr(LOG, HOME, "submit login form", "member"),
    tr(HOME, HELP, "click link 'Help'", "guest"),
    tr(HOME, HELP, "click link 'Help'", "member"),
]


@pytest.fixture()
def raw_path(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in LINES) + "\n")
    return path


CLUSTER_JSON = {
    "capabilities": [
        {
            "name": "brew_logging",
            "user_goal": "Record a coffee brew",
            "screen_ids": [HOME, LOG],
            "entry_screen_ids": [HOME],
            "implicit_metric": "entries per week",
        },
        {
            "name": "authentication",
            "user_goal": "Sign in",
            "screen_ids": [LOGIN],
            "entry_screen_ids": [LOGIN],
            "implicit_metric": None,
        },
    ],
    "unassigned_screen_ids": [HELP],
}


class FakeLLM:
    """Canned LLM: returns queued responses, records every prompt."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, max_tokens=4096):
        self.calls.append((system, user))
        return self.responses.pop(0)


# --------------------------------------------------------------------------
# Graph loading
# --------------------------------------------------------------------------


def test_graph_merges_roles_and_dedupes(raw_path):
    graph = load_raw_jsonl(raw_path)
    assert set(graph.screens) == {HOME, LOG, LOGIN, HELP}
    assert graph.screens[HOME].roles == {"guest", "member"}
    assert graph.screens[HOME].headings == ["Your brews, remembered", "Recent brews"]
    help_edge = graph.edges[(HOME, HELP, "click link 'Help'")]
    assert help_edge.roles == {"guest", "member"}
    assert graph.roles == ["guest", "member"]
    assert graph.run_date == "2026-06-11"


def test_graph_rejects_wrong_schema(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text(json.dumps({"kind": "meta", "schema": 1}) + "\n")
    with pytest.raises(ValueError, match="schema"):
        load_raw_jsonl(path)


def test_graph_rejects_empty(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text(json.dumps(META) + "\n")
    with pytest.raises(ValueError, match="No observations"):
        load_raw_jsonl(path)


# --------------------------------------------------------------------------
# Clustering validation
# --------------------------------------------------------------------------


def test_cluster_valid_response(raw_path):
    graph = load_raw_jsonl(raw_path)
    llm = FakeLLM([json.dumps(CLUSTER_JSON)])
    result = synthesize_clusters(graph, llm)
    assert [c.name for c in result.capabilities] == ["brew_logging", "authentication"]
    assert result.unassigned_screen_ids == [HELP]


def test_cluster_tolerates_code_fences(raw_path):
    graph = load_raw_jsonl(raw_path)
    llm = FakeLLM(["```json\n" + json.dumps(CLUSTER_JSON) + "\n```"])
    assert len(synthesize_clusters(graph, llm).capabilities) == 2


def test_cluster_accepts_single_capability(raw_path):
    # A scoped walk of a small app can be one coherent capability.
    graph = load_raw_jsonl(raw_path)
    single = {
        "capabilities": [
            {
                "name": "brewing",
                "user_goal": "Everything brews",
                "screen_ids": [HOME, LOG, LOGIN],
                "entry_screen_ids": [HOME],
                "implicit_metric": None,
            }
        ],
        "unassigned_screen_ids": [HELP],
    }
    llm = FakeLLM([json.dumps(single)])
    result = synthesize_clusters(graph, llm, retries=0)
    assert [c.name for c in result.capabilities] == ["brewing"]


def test_cluster_repairs_coverage_only_failures(raw_path):
    # The LLM dropped HELP entirely: after retries, the dropped screen is
    # moved to unassigned instead of failing the run.
    graph = load_raw_jsonl(raw_path)
    dropped = json.loads(json.dumps(CLUSTER_JSON))
    dropped["unassigned_screen_ids"] = []
    llm = FakeLLM([json.dumps(dropped)])
    result = synthesize_clusters(graph, llm, retries=0)
    assert result.unassigned_screen_ids == [HELP]
    assert [c.name for c in result.capabilities] == ["brew_logging", "authentication"]


def test_cluster_does_not_repair_mixed_failures(raw_path):
    # Coverage repair only applies when missing screens are the ONLY defect.
    graph = load_raw_jsonl(raw_path)
    bad = json.loads(json.dumps(CLUSTER_JSON))
    bad["unassigned_screen_ids"] = []
    bad["capabilities"][1]["screen_ids"].append(HOME)  # overlap too
    llm = FakeLLM([json.dumps(bad)])
    with pytest.raises(ValueError, match="failed validation"):
        synthesize_clusters(graph, llm, retries=0)


def test_cluster_retries_with_errors_then_fails(raw_path):
    graph = load_raw_jsonl(raw_path)
    bad = json.dumps({"capabilities": [], "unassigned_screen_ids": []})
    llm = FakeLLM([bad, bad])
    with pytest.raises(ValueError, match="failed validation"):
        synthesize_clusters(graph, llm, retries=1)
    assert len(llm.calls) == 2
    assert "previous response was invalid" in llm.calls[1][1]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["capabilities"][0]["screen_ids"].append("/nope::s9"), "unknown screen ids"),
        (lambda d: d["capabilities"][1]["screen_ids"].append(HOME), "already assigned"),
        (lambda d: d["capabilities"][0].update(name="Brew Logging!"), "lowercase_slug"),
        (
            lambda d: d["capabilities"][0].update(entry_screen_ids=[LOGIN]),
            "entry screens not in screen_ids",
        ),
    ],
)
def test_cluster_rejects_invalid(raw_path, mutate, message):
    graph = load_raw_jsonl(raw_path)
    bad = json.loads(json.dumps(CLUSTER_JSON))
    mutate(bad)
    llm = FakeLLM([json.dumps(bad)])
    with pytest.raises(ValueError, match="failed validation"):
        synthesize_clusters(graph, llm, retries=0)


# --------------------------------------------------------------------------
# Reverse-Figma
# --------------------------------------------------------------------------


def test_figma_components_from_triggers_and_headings(raw_path):
    graph = load_raw_jsonl(raw_path)
    cluster = CapabilityCluster(
        name="brew_logging", user_goal="", screen_ids=[HOME, LOG], entry_screen_ids=[HOME]
    )
    figma = synthesize_reverse_figma(cluster, graph)
    home = next(s for s in figma.screens if s.id == HOME)
    roles_and_text = [(c.role, c.text) for c in home.components]
    assert ("heading", "Your brews, remembered") in roles_and_text
    assert ("button", "New entry") in roles_and_text
    assert ("link", "Sign In") in roles_and_text
    # Screen seen by both roles -> no single role on the frame
    assert home.role is None
    # Member-only button on a shared screen is role-gated
    new_entry = next(c for c in home.components if c.text == "New entry")
    assert new_entry.visible_to_roles == ["member"]
    # LOG was only seen by member
    log = next(s for s in figma.screens if s.id == LOG)
    assert log.role == "member"


def test_figma_transitions_stay_inside_cluster(raw_path):
    graph = load_raw_jsonl(raw_path)
    cluster = CapabilityCluster(
        name="brew_logging", user_goal="", screen_ids=[HOME, LOG], entry_screen_ids=[HOME]
    )
    figma = synthesize_reverse_figma(cluster, graph)
    pairs = {(t.from_screen, t.to_screen) for t in figma.transitions}
    assert pairs == {(HOME, LOG), (LOG, HOME)}
    gated = next(t for t in figma.transitions if t.to_screen == LOG)
    assert gated.role_gated == ["member"]


# --------------------------------------------------------------------------
# Reverse-PRD evidence
# --------------------------------------------------------------------------


def test_prd_evidence_includes_screens_and_exits(raw_path):
    graph = load_raw_jsonl(raw_path)
    cluster = CapabilityCluster(
        name="brew_logging", user_goal="Record a brew", screen_ids=[HOME, LOG],
        entry_screen_ids=[HOME],
    )
    llm = FakeLLM(["# PRD"])
    assert synthesize_reverse_prd(cluster, graph, llm) == "# PRD"
    _system, user = llm.calls[0]
    assert "CAPABILITY: brew_logging" in user
    assert "Your brews, remembered" in user
    assert "EXITS INTO OTHER CAPABILITIES" in user  # Home -> Login / Help leave the cluster
    assert "click link 'Sign In'" in user


# --------------------------------------------------------------------------
# Pack assembly
# --------------------------------------------------------------------------


def test_synthesize_pack_end_to_end(raw_path, tmp_path):
    graph = load_raw_jsonl(raw_path)
    llm = FakeLLM([json.dumps(CLUSTER_JSON), "# PRD brew", "# PRD auth"])
    pack = synthesize_pack(graph, llm, customer_id="brewlog", snapshot_version="2026-W24")

    assert isinstance(pack, FeaturePack)
    assert pack.manifest.capabilities == ["brew_logging", "authentication"]
    assert pack.manifest.crawl_metadata["screens"] == "4"
    assert pack.manifest.crawl_metadata["unassigned_screens"] == "1"
    brew = pack.capabilities[0]
    assert brew.surfaces == ["/", "/log"]
    assert brew.entry_points == ["/"]
    assert brew.roles == ["guest", "member"]
    assert brew.implicit_metric == "entries per week"
    assert brew.reverse_prd == "# PRD brew"

    store = LocalArtifactStore(tmp_path / "out")
    written = Path(store.write_pack(pack))
    manifest = FeaturePackManifest.model_validate_json((written / "manifest.json").read_text())
    assert manifest.snapshot_version == "2026-W24"
    figma = ReverseFigma.model_validate_json(
        (written / "capabilities" / "brew_logging" / "reverse-figma.json").read_text()
    )
    assert {s.id for s in figma.screens} == {HOME, LOG}
    # The latest pointer is the publish step, separate from write_pack so a
    # snapshot only becomes visible once every artifact (pack + diff) exists.
    # It's what the simulator's FeaturePackLoader resolves before reading.
    assert not (written.parent / "latest").exists()
    assert publish_latest(store, "brewlog", "2026-W24") is True
    assert (written.parent / "latest").read_text() == "2026-W24"
    assert publish_latest(store, "brewlog", "2026-W25") is True
    assert (written.parent / "latest").read_text() == "2026-W25"
    # A backfill of an older snapshot must not regress the pointer.
    assert publish_latest(store, "brewlog", "2026-W24") is False
    assert (written.parent / "latest").read_text() == "2026-W25"


def test_weekly_bucket():
    assert weekly_bucket("2026-06-11") == "2026-W24"
    assert weekly_bucket("2026-01-01") == "2026-W01"


# --------------------------------------------------------------------------
# Honest-test gate (structure-only comparison)
# --------------------------------------------------------------------------


def _pack_dict(cap_screens: dict[str, list[dict]], transitions=None):
    return {
        "manifest": {"capabilities": list(cap_screens)},
        "capabilities": {
            name: {"screens": screens, "transitions": transitions or []}
            for name, screens in cap_screens.items()
        },
    }


def _screen(sid, name, words=""):
    return {"id": sid, "name": name, "components": [{"id": "c", "text": words}]}


def test_compare_converging_structures_pass():
    candidate = _pack_dict(
        {
            "watching": [_screen("/::sa1", "Home", "browse"), _screen("/login::sa2", "Sign in")],
            "plans": [_screen("/plan::sa3", "Pick a plan", "upgrade")],
        }
    )
    reference = _pack_dict(
        {
            "viewing": [_screen("home", "Start watching", "browse"), _screen("auth", "Log in")],
            "billing": [_screen("plans", "Choose your plan", "pricing")],
        }
    )
    report = psc.judge(candidate, reference)
    assert report["checks"]["category_similarity"]["pass"]
    assert report["checks"]["no_content_leakage"]["pass"]
    assert report["verdict"] == "PASS"


def test_compare_fails_on_screen_id_leak():
    candidate = _pack_dict({"a": [_screen("home", "Home", "browse")]})
    reference = _pack_dict({"b": [_screen("home", "Start", "browse")]})
    report = psc.judge(candidate, reference)
    assert not report["checks"]["no_content_leakage"]["pass"]
    assert report["verdict"] == "FAIL"


def test_compare_fails_on_divergent_categories():
    candidate = _pack_dict({"a": [_screen("/x::s1", "Checkout", "payment billing")]})
    reference = _pack_dict({"b": [_screen("help", "Help center", "faq support")]})
    report = psc.judge(candidate, reference)
    assert not report["checks"]["category_similarity"]["pass"]
    assert report["verdict"] == "FAIL"


def test_compare_fails_on_capability_count_blowout():
    candidate = _pack_dict(
        {f"cap{i}": [_screen(f"/x{i}::s{i}", "Browse", "browse")] for i in range(8)}
    )
    reference = _pack_dict({"one": [_screen("home", "Browse", "browse")]})
    report = psc.judge(candidate, reference)
    assert not report["checks"]["capability_count_ratio"]["pass"]
    assert report["verdict"] == "FAIL"


def test_compare_sample_scope_gates_on_containment():
    # Full-product candidate vs tiny sample reference: symmetric similarity
    # and count ratio would fail, but containment holds.
    candidate = _pack_dict(
        {
            f"cap{i}": [_screen(f"/x{i}::s{i}", name, words)]
            for i, (name, words) in enumerate(
                [
                    ("Sign in", ""),
                    ("Browse", "discover"),
                    ("My library", "saved"),
                    ("Watch", "play"),
                    ("Plans", "pricing"),
                    ("Help", "faq"),
                    ("Settings", "preferences"),
                    ("Search", "results"),
                ]
            )
        }
    )
    reference = _pack_dict(
        {
            "auth": [_screen("ref-auth", "Log in")],
            "viewing": [_screen("ref-watch", "Play episode"), _screen("ref-lib", "Watchlist")],
        }
    )
    report = psc.judge(candidate, reference, reference_scope="sample")
    assert report["checks"]["category_recall"]["value"] == 1.0
    assert report["verdict"] == "PASS"
    assert "capability_count_ratio" in report["info"]
    # The same packs fail under full scope — the scope flag is load-bearing.
    assert psc.judge(candidate, reference)["verdict"] == "FAIL"


def test_compare_sample_scope_fails_on_missing_categories():
    candidate = _pack_dict({"a": [_screen("/x::s1", "Browse", "discover")]})
    reference = _pack_dict(
        {
            "b": [
                _screen("ref1", "Log in"),
                _screen("ref2", "Choose plan"),
                _screen("ref3", "Search results"),
                _screen("ref4", "Browse"),
            ]
        }
    )
    report = psc.judge(candidate, reference, reference_scope="sample")
    assert report["checks"]["category_recall"]["value"] == 0.25
    assert report["verdict"] == "FAIL"


def test_compare_sample_scope_still_fails_on_leak():
    candidate = _pack_dict({"a": [_screen("home", "Home", "browse")]})
    reference = _pack_dict({"b": [_screen("home", "Start", "browse")]})
    report = psc.judge(candidate, reference, reference_scope="sample")
    assert report["verdict"] == "FAIL"


def test_categorize_screen_buckets():
    assert psc.categorize_screen(_screen("/login::s1", "Sign in")) == "auth"
    assert psc.categorize_screen(_screen("/account/plan::s2", "Choose plan")) == "checkout_plan"
    assert psc.categorize_screen(_screen("/x::s3", "Mystery")) == "other"
