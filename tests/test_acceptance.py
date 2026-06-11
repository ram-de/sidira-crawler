"""Hermetic tests for the sandbox acceptance judge (scripts/sandbox_acceptance.py).

No network, no sandbox, no browser: every test feeds synthetic walk dicts to
the pure judge functions. The harness/orchestration side (docker, server
processes) is exercised by the real acceptance run, not here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sandbox_acceptance.py"
_spec = importlib.util.spec_from_file_location("sandbox_acceptance", _SCRIPT)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)


# --------------------------------------------------------------------------
# Synthetic walk builders
# --------------------------------------------------------------------------


def obs(screen_id, url="http://x/", **extra):
    base = {
        "url": url,
        "route": sa.screen_id_parts(screen_id)[0],
        "title": "",
        "role": "member",
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


def tr(from_id, to_id, trigger):
    return {"from_screen_id": from_id, "to_screen_id": to_id, "trigger": trigger, "role": "member"}


def walk(observations=(), transitions=()):
    return {
        "capability_name": "",
        "role": "member",
        "observations": list(observations),
        "transitions": list(transitions),
    }


# --------------------------------------------------------------------------
# screen_id parsing / route normalization
# --------------------------------------------------------------------------


def test_screen_id_parts_splits_route_and_flags_and_drops_digest():
    route, flags = sa.screen_id_parts("/browse::overlay+exhausted+s1ce809")
    assert route == "/browse"
    assert flags == {"overlay", "exhausted"}


def test_screen_id_parts_plain_digest_only():
    route, flags = sa.screen_id_parts("/help::s17706b")
    assert route == "/help"
    assert flags == set()


def test_normalize_route_collapses_id_segments_and_strips_query():
    assert sa.normalize_route("/watch/t-101") == "/watch/:id"
    assert sa.normalize_route("/browse?page=2") == "/browse"
    assert sa.normalize_route("/") == "/"


# --------------------------------------------------------------------------
# Observation matchers
# --------------------------------------------------------------------------


def test_match_observation_route():
    assert sa.match_observation({"route": "/help"}, obs("/help::s17706b"))
    assert not sa.match_observation({"route": "/help"}, obs("/browse::s17706b"))


def test_match_observation_flags_present_and_absent():
    o = obs("/login::login+s401308")
    assert sa.match_observation({"flags": ["login"]}, o)
    assert not sa.match_observation({"flags": ["dialog"]}, o)
    assert not sa.match_observation({"flags_absent": ["login"]}, o)
    assert sa.match_observation({"flags_absent": ["dialog"]}, o)


def test_match_observation_url_contains():
    o = obs("/login::login+s1", url="http://x/login?next=%2Fbrowse")
    assert sa.match_observation({"url_contains": "next="}, o)
    assert not sa.match_observation({"url_contains": "nope="}, o)


def test_match_observation_testid_present_and_absent():
    o = obs("/browse::s1", data_testids=["end-of-catalog"])
    assert sa.match_observation({"testid": "end-of-catalog"}, o)
    assert not sa.match_observation({"testid": "hero"}, o)
    assert not sa.match_observation({"testid_absent": "end-of-catalog"}, o)
    assert sa.match_observation({"testid_absent": "hero"}, o)


def test_match_observation_copy_regex_spans_headings_and_copy():
    o = obs("/::s1", headings=["Unlimited stories. One small price."], visible_copy=["Sign In"])
    assert sa.match_observation({"copy_regex": r"Unlimited stories\."}, o)
    assert sa.match_observation({"copy_regex": "Sign In"}, o)
    assert not sa.match_observation({"copy_regex": "Order confirmed!"}, o)


def test_match_observation_all_predicates_are_anded():
    o = obs("/browse::s1", data_testids=["end-of-catalog"])
    assert not sa.match_observation({"route": "/browse", "testid": "end-of-catalog",
                                     "copy_regex": "absent"}, o)


def test_match_screen_returns_evidence_or_none():
    w = walk([obs("/help::s1"), obs("/browse::s2")])
    assert sa.match_screen({"route": "/browse"}, w) == "/browse::s2"
    assert sa.match_screen({"route": "/account"}, w) is None


# --------------------------------------------------------------------------
# Generic transition matcher
# --------------------------------------------------------------------------


def test_match_transition_generic_trigger_and_routes():
    w = walk(transitions=[tr("/a::s1", "/b::s2", "click link 'Go'")])
    m = {"kind": "transition", "trigger_regex": "click link", "from_route": "/a",
         "to_route": "/b"}
    assert sa.match_transition(m, w) is not None
    assert sa.match_transition({**m, "to_route": "/c"}, w) is None
    assert sa.match_transition({**m, "trigger_regex": "scroll"}, w) is None


def test_match_transition_unknown_kind_raises():
    with pytest.raises(ValueError):
        sa.match_transition({"kind": "vibes"}, walk())


# --------------------------------------------------------------------------
# login-roundtrip matcher
# --------------------------------------------------------------------------


def _roundtrip_walk(login_url, profiles_url, downstream_ok=True):
    observations = [
        obs("/login::login+s1", url=login_url),
        obs("/profiles::chooser+s2", url=profiles_url),
        obs("/browse::s3", url="http://x/browse"),
    ]
    transitions = [tr("/login::login+s1", "/profiles::chooser+s2", "submit login form")]
    if downstream_ok:
        transitions.append(tr("/profiles::chooser+s2", "/browse::s3", "select first option 'p'"))
    return walk(observations, transitions)


MATCHER = {"kind": "login-roundtrip", "trigger": "submit login form", "param": "next"}


def test_login_roundtrip_next_on_login_wall_url():
    w = _roundtrip_walk("http://x/login?next=%2Fbrowse", "http://x/profiles")
    assert sa.match_login_roundtrip(MATCHER, w) is not None


def test_login_roundtrip_next_forwarded_to_landing_screen():
    # The app forwards the sanitized next to its post-login interstitial.
    w = _roundtrip_walk("http://x/login", "http://x/profiles?next=%2Fbrowse")
    assert sa.match_login_roundtrip(MATCHER, w) is not None


def test_login_roundtrip_fails_without_next_param():
    w = _roundtrip_walk("http://x/login", "http://x/profiles")
    assert sa.match_login_roundtrip(MATCHER, w) is None


def test_login_roundtrip_fails_when_target_not_reachable_downstream():
    # next= present, but the walk never proceeds from /profiles to /browse.
    w = _roundtrip_walk("http://x/login?next=%2Fbrowse", "http://x/profiles",
                        downstream_ok=False)
    assert sa.match_login_roundtrip(MATCHER, w) is None


def test_login_roundtrip_requires_login_flag_on_from_screen():
    w = walk(
        [
            obs("/signup::s1", url="http://x/signup?next=%2Fbrowse"),
            obs("/browse::s3", url="http://x/browse"),
        ],
        [tr("/signup::s1", "/browse::s3", "submit login form")],
    )
    assert sa.match_login_roundtrip(MATCHER, w) is None


def test_login_roundtrip_reaches_target_through_multiple_hops():
    w = walk(
        [
            obs("/login::login+s1", url="http://x/login?next=%2Fwatch%2Ft-101"),
            obs("/profiles::chooser+s2", url="http://x/profiles?next=%2Fwatch%2Ft-101"),
            obs("/browse::s3", url="http://x/browse"),
            obs("/watch/:id::s4", url="http://x/watch/t-101"),
        ],
        [
            tr("/login::login+s1", "/profiles::chooser+s2", "submit login form"),
            tr("/profiles::chooser+s2", "/browse::s3", "select first option 'p'"),
            tr("/browse::s3", "/watch/:id::s4", "click link 'Play'"),
        ],
    )
    evidence = sa.match_login_roundtrip(MATCHER, w)
    assert evidence is not None
    assert "/watch/:id" in evidence


def test_login_roundtrip_does_not_loop_on_cyclic_graphs():
    w = walk(
        [
            obs("/login::login+s1", url="http://x/login?next=%2Fnowhere"),
            obs("/a::s2", url="http://x/a"),
            obs("/b::s3", url="http://x/b"),
        ],
        [
            tr("/login::login+s1", "/a::s2", "submit login form"),
            tr("/a::s2", "/b::s3", "click link 'b'"),
            tr("/b::s3", "/a::s2", "click link 'a'"),
        ],
    )
    assert sa.match_login_roundtrip(MATCHER, w) is None  # terminates, no match


# --------------------------------------------------------------------------
# scroll-growth matcher
# --------------------------------------------------------------------------

SCROLL = {"kind": "scroll-growth", "trigger": "scroll feed", "route": "/browse"}


def test_scroll_growth_matches_state_changing_scroll():
    w = walk(transitions=[tr("/browse::s1", "/browse::exhausted+s2", "scroll feed")])
    assert sa.match_scroll_growth(SCROLL, w) is not None


def test_scroll_growth_rejects_noop_scroll():
    w = walk(transitions=[tr("/browse::s1", "/browse::s1", "scroll feed")])
    assert sa.match_scroll_growth(SCROLL, w) is None


def test_scroll_growth_respects_route():
    w = walk(transitions=[tr("/kids::s1", "/kids::exhausted+s2", "scroll feed")])
    assert sa.match_scroll_growth(SCROLL, w) is None
    assert sa.match_scroll_growth({**SCROLL, "route": "/kids"}, w) is not None


def test_scroll_growth_requires_scroll_trigger():
    w = walk(transitions=[tr("/browse::s1", "/browse::s2", "click link 'More'")])
    assert sa.match_scroll_growth(SCROLL, w) is None


# --------------------------------------------------------------------------
# Coverage thresholds / mustDiscover / determinism / verdict
# --------------------------------------------------------------------------

MANIFEST = {
    "screens": [
        {"id": "a", "route": "/a", "state": "default", "roles": ["guest", "member"]},
        {"id": "b", "route": "/b", "state": "default", "roles": ["member"]},
        {"id": "c", "route": "/c", "state": "default", "roles": ["member"]},
        {"id": "d", "route": "/d", "state": "default", "roles": ["member"]},
        {"id": "e", "route": "/e", "state": "default", "roles": ["member"]},
    ],
    "transitions": [
        {"id": "t-scroll", "from": "a", "to": "b", "roles": ["member"]},
    ],
    "mustDiscover": [
        {"id": "b", "kind": "screen", "why": "test screen"},
        {"id": "t-scroll", "kind": "transition", "why": "test transition"},
    ],
}

CONFIG = {
    "determinism_role": "member",
    "roles": {
        "guest": {"username": None, "password": None, "threshold": 0.8},
        "member": {"username": "m", "password": "p", "threshold": 0.8},
    },
    "screen_matchers": {
        "a": {"route": "/a"},
        "b": {"route": "/b"},
        "c": {"route": "/c"},
        "d": {"route": "/d"},
        "e": {"route": "/e"},
    },
    "transition_matchers": {
        "t-scroll": {"kind": "scroll-growth", "trigger": "scroll feed", "route": "/a"},
    },
}


def _full_member_walk():
    return walk(
        [obs(f"/{s}::s1") for s in "abcde"],
        [tr("/a::s1", "/b::s2", "scroll feed")],
    )


def test_evaluate_role_threshold_pass_at_exact_boundary():
    # 4/5 = 0.8 meets the 0.8 threshold exactly.
    w = walk([obs(f"/{s}::s1") for s in "abcd"])
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", w)
    assert result["matched"] == 4 and result["total"] == 5
    assert result["passed"] is True


def test_evaluate_role_threshold_fail_below_boundary():
    w = walk([obs(f"/{s}::s1") for s in "abc"])
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", w)
    assert result["ratio"] == pytest.approx(0.6)
    assert result["passed"] is False


def test_evaluate_role_only_counts_screens_visible_to_role():
    w = walk([obs("/a::s1")])
    result = sa.evaluate_role(MANIFEST, CONFIG, "guest", w)
    assert result["total"] == 1 and result["passed"] is True


def test_evaluate_role_crashed_walk_scores_zero():
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", None)
    assert result["matched"] == 0 and result["passed"] is False


def test_must_discover_found_and_missing():
    results = sa.evaluate_must_discover(MANIFEST, CONFIG, {"member": _full_member_walk()})
    assert all(r["found"] for r in results)
    # Remove the scroll transition: t-scroll must report missing.
    bare = walk([obs(f"/{s}::s1") for s in "abcde"])
    results = sa.evaluate_must_discover(MANIFEST, CONFIG, {"member": bare})
    by_id = {r["id"]: r for r in results}
    assert by_id["b"]["found"] is True
    assert by_id["t-scroll"]["found"] is False


def test_missing_must_discover_fails_verdict_despite_full_coverage():
    # Coverage 5/5 for member, 1/1 for guest — but the mustDiscover transition
    # is absent, so the overall verdict must fail.
    member = walk([obs(f"/{s}::s1") for s in "abcde"])
    guest = walk([obs("/a::s1")])
    verdict = sa.judge(MANIFEST, CONFIG, {"guest": guest, "member": member}, member, {})
    assert all(r["passed"] for r in verdict["roles"])
    assert verdict["passed"] is False


def test_compare_determinism_identical_sets_pass():
    first = walk([obs("/a::s1"), obs("/b::s2")])
    second = walk([obs("/b::s2"), obs("/a::s1")])  # order must not matter
    assert sa.compare_determinism(first, second)["passed"] is True


def test_compare_determinism_mismatch_reports_both_sides():
    first = walk([obs("/a::s1"), obs("/b::s2")])
    second = walk([obs("/a::s1"), obs("/c::s3")])
    result = sa.compare_determinism(first, second)
    assert result["passed"] is False
    assert result["only_first"] == ["/b::s2"]
    assert result["only_second"] == ["/c::s3"]


def test_compare_determinism_crashed_walk_is_not_comparable():
    result = sa.compare_determinism(walk([obs("/a::s1")]), None)
    assert result["passed"] is False and result["comparable"] is False


def test_judge_passes_when_everything_holds():
    member = _full_member_walk()
    guest = walk([obs("/a::s1")])
    verdict = sa.judge(MANIFEST, CONFIG, {"guest": guest, "member": member}, member, {})
    assert verdict["passed"] is True


def test_judge_fails_on_walk_crash():
    member = _full_member_walk()
    guest = walk([obs("/a::s1")])
    verdict = sa.judge(
        MANIFEST, CONFIG, {"guest": guest, "member": member}, member,
        {"premium": "RuntimeError: boom"},
    )
    assert verdict["passed"] is False


def test_judge_fails_on_determinism_mismatch():
    member = _full_member_walk()
    guest = walk([obs("/a::s1")])
    rewalk = walk([obs(f"/{s}::s1") for s in "abcd"], [tr("/a::s1", "/b::s2", "scroll feed")])
    verdict = sa.judge(MANIFEST, CONFIG, {"guest": guest, "member": member}, rewalk, {})
    assert verdict["determinism"]["passed"] is False
    assert verdict["passed"] is False


def test_validate_config_rejects_matcher_gaps():
    incomplete = {**CONFIG, "screen_matchers": {"a": {"route": "/a"}}}
    with pytest.raises(SystemExit):
        sa.validate_config(MANIFEST, incomplete)
    no_transition = {**CONFIG, "transition_matchers": {}}
    with pytest.raises(SystemExit):
        sa.validate_config(MANIFEST, no_transition)


# --------------------------------------------------------------------------
# The shipped judge config stays in lockstep with the shipped manifest schema
# --------------------------------------------------------------------------


def test_shipped_config_parses_and_has_known_matcher_kinds():
    import json

    config_path = Path(__file__).resolve().parent.parent / "acceptance" / "streamly-sandbox.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(config["roles"]) == {"guest", "member", "premium"}
    for matcher in config["screen_matchers"].values():
        assert matcher.get("kind", "observation") == "observation"
    for matcher in config["transition_matchers"].values():
        assert matcher["kind"] in {"transition", "login-roundtrip", "scroll-growth"}
