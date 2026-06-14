"""Hermetic tests for the sandbox acceptance judge (scripts/sandbox_acceptance.py).

No network, no sandbox, no browser: every test feeds synthetic walk dicts to
the pure judge functions. The harness/orchestration side (docker, server
processes) is exercised by the real acceptance run, not here.
"""

from __future__ import annotations

import importlib.util
import json
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

# Evidence-free variant: exercises the trigger/route/digest-change logic alone
# (validate_config refuses to ship a scroll-growth matcher without to_* keys).
SCROLL = {"kind": "scroll-growth", "trigger": "scroll feed", "route": "/browse"}
SCROLL_EVIDENCE = {**SCROLL, "to_copy_regex": "Trending Now"}


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


def test_scroll_growth_digest_change_alone_is_not_evidence():
    # The to-screen digest changed, but the to-observation carries none of the
    # lazy content the matcher demands — any DOM mutation could do that.
    w = walk(
        [obs("/browse::s1"), obs("/browse::exhausted+s2", visible_copy=["same rows"])],
        [tr("/browse::s1", "/browse::exhausted+s2", "scroll feed")],
    )
    assert sa.match_scroll_growth(SCROLL_EVIDENCE, w) is None


def test_scroll_growth_to_copy_evidence_in_to_observation_matches():
    w = walk(
        [obs("/browse::s1"), obs("/browse::exhausted+s2", visible_copy=["Trending Now"])],
        [tr("/browse::s1", "/browse::exhausted+s2", "scroll feed")],
    )
    assert sa.match_scroll_growth(SCROLL_EVIDENCE, w) is not None


def test_scroll_growth_to_evidence_on_from_observation_does_not_count():
    w = walk(
        [obs("/browse::s1", visible_copy=["Trending Now"]), obs("/browse::exhausted+s2")],
        [tr("/browse::s1", "/browse::exhausted+s2", "scroll feed")],
    )
    assert sa.match_scroll_growth(SCROLL_EVIDENCE, w) is None


def test_scroll_growth_missing_to_observation_does_not_match():
    w = walk([obs("/browse::s1")], [tr("/browse::s1", "/browse::exhausted+s2", "scroll feed")])
    assert sa.match_scroll_growth(SCROLL_EVIDENCE, w) is None


def test_scroll_growth_to_testid_and_to_flags_evidence():
    w = walk(
        [obs("/browse::exhausted+s2", data_testids=["end-of-catalog"])],
        [tr("/browse::s1", "/browse::exhausted+s2", "scroll feed")],
    )
    assert sa.match_scroll_growth({**SCROLL, "to_testid": "end-of-catalog"}, w) is not None
    assert sa.match_scroll_growth({**SCROLL, "to_flags": ["exhausted"]}, w) is not None
    assert sa.match_scroll_growth({**SCROLL, "to_testid": "hero"}, w) is None
    assert sa.match_scroll_growth({**SCROLL, "to_flags": ["dialog"]}, w) is None


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
        "t-scroll": {"kind": "scroll-growth", "trigger": "scroll feed", "route": "/a",
                     "to_copy_regex": "Lazy row"},
    },
}


def _full_member_walk():
    return walk(
        [obs(f"/{s}::s1") for s in "acde"] + [obs("/b::s2", visible_copy=["Lazy row"])],
        [tr("/a::s1", "/b::s2", "scroll feed")],
    )


def test_evaluate_role_threshold_pass_at_exact_boundary():
    # 4/5 = 0.8 meets the 0.8 threshold exactly.
    w = walk([obs(f"/{s}::s1") for s in "abcd"])
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", {"/": w})
    assert result["matched"] == 4 and result["total"] == 5
    assert result["passed"] is True


def test_evaluate_role_threshold_fail_below_boundary():
    w = walk([obs(f"/{s}::s1") for s in "abc"])
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", {"/": w})
    assert result["ratio"] == pytest.approx(0.6)
    assert result["passed"] is False


def test_evaluate_role_only_counts_screens_visible_to_role():
    w = walk([obs("/a::s1")])
    result = sa.evaluate_role(MANIFEST, CONFIG, "guest", {"/": w})
    assert result["total"] == 1 and result["passed"] is True


def test_evaluate_role_crashed_walk_scores_zero():
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", None)
    assert result["matched"] == 0 and result["passed"] is False
    # All of a role's walks crashed -> empty dict scores the same way.
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", {})
    assert result["matched"] == 0 and result["passed"] is False


def test_evaluate_role_merges_screen_coverage_across_entry_paths():
    # 3 screens from "/" and 2 different ones from "/b": together 5/5.
    first = walk([obs(f"/{s}::s1") for s in "abc"])
    second = walk([obs(f"/{s}::s1") for s in "de"])
    result = sa.evaluate_role(MANIFEST, CONFIG, "member", {"/": first, "/b": second})
    assert result["matched"] == 5 and result["passed"] is True


def test_must_discover_found_and_missing():
    results = sa.evaluate_must_discover(MANIFEST, CONFIG, {"member": {"/": _full_member_walk()}})
    assert all(r["found"] for r in results)
    # Remove the scroll transition: t-scroll must report missing.
    bare = walk([obs(f"/{s}::s1") for s in "abcde"])
    results = sa.evaluate_must_discover(MANIFEST, CONFIG, {"member": {"/": bare}})
    by_id = {r["id"]: r for r in results}
    assert by_id["b"]["found"] is True
    assert by_id["t-scroll"]["found"] is False


def test_must_discover_transition_found_in_any_single_walk():
    # The scroll transition only happens in the secondary entry path's walk.
    bare = walk([obs(f"/{s}::s1") for s in "abcde"])
    scrolly = walk([obs("/a::s1"), obs("/b::s2", visible_copy=["Lazy row"])],
                   [tr("/a::s1", "/b::s2", "scroll feed")])
    results = sa.evaluate_must_discover(
        MANIFEST, CONFIG, {"member": {"/": bare, "/a": scrolly}}
    )
    by_id = {r["id"]: r for r in results}
    assert by_id["t-scroll"]["found"] is True


def test_missing_must_discover_fails_verdict_despite_full_coverage():
    # Coverage 5/5 for member, 1/1 for guest — but the mustDiscover transition
    # is absent, so the overall verdict must fail.
    member = walk([obs(f"/{s}::s1") for s in "abcde"])
    guest = walk([obs("/a::s1")])
    verdict = sa.judge(
        MANIFEST, CONFIG, {"guest": {"/": guest}, "member": {"/": member}}, member, {}
    )
    assert all(r["passed"] for r in verdict["roles"])
    assert verdict["passed"] is False


# --------------------------------------------------------------------------
# Entry paths and walk merging
# --------------------------------------------------------------------------


def test_role_entry_paths_defaults_and_explicit():
    config = {"entry_path": "/start", "roles": {"guest": {}, "member": {"entry_paths": ["/", "/a"]}}}
    assert sa.role_entry_paths(config, "guest") == ["/start"]
    assert sa.role_entry_paths(config, "member") == ["/", "/a"]
    assert sa.role_entry_paths({"roles": {"guest": {}}}, "guest") == ["/"]


def test_role_entry_specs_plain_strings_use_default_depth():
    config = {"depth": 6, "roles": {"member": {"entry_paths": ["/", "/a"]}}}
    assert sa.role_entry_specs(config, "member") == [("/", 6), ("/a", 6)]
    # Config-wide depth defaults to 6 when absent.
    assert sa.role_entry_specs({"roles": {"member": {"entry_paths": ["/"]}}}, "member") == [("/", 6)]


def test_role_entry_specs_dict_entries_override_depth_per_walk():
    config = {"depth": 6, "roles": {"member": {"entry_paths": [
        "/", {"path": "/account/plan", "depth": 5}, {"path": "/x"}]}}}
    assert sa.role_entry_specs(config, "member") == [
        ("/", 6), ("/account/plan", 5), ("/x", 6)]
    # role_entry_paths drops the depths but keeps order.
    assert sa.role_entry_paths(config, "member") == ["/", "/account/plan", "/x"]


def test_role_entry_specs_default_entry_path_uses_default_depth():
    config = {"entry_path": "/start", "depth": 4, "roles": {"guest": {}}}
    assert sa.role_entry_specs(config, "guest") == [("/start", 4)]


def test_merge_walks_unions_observations_and_transitions():
    first = walk([obs("/a::s1"), obs("/b::s2")], [tr("/a::s1", "/b::s2", "click link 'b'")])
    second = walk([obs("/a::s1"), obs("/c::s3")], [tr("/a::s1", "/b::s2", "click link 'b'"),
                                                   tr("/a::s1", "/c::s3", "click link 'c'")])
    merged = sa.merge_walks({"/": first, "/c": second})
    assert {o["screen_id"] for o in merged["observations"]} == {"/a::s1", "/b::s2", "/c::s3"}
    assert len(merged["observations"]) == 3  # deduped by (screen_id, url)
    assert len(merged["transitions"]) == 2  # deduped by (from, to, trigger)


def test_merge_walks_keeps_url_variants_of_the_same_screen_id():
    # The login wall (/login?next=…) digests to the same screen_id as the
    # plain /login page. Merging must keep BOTH observations: deduping by
    # screen_id alone would let the first walk's plain variant shadow the
    # wall variant, and a url_contains matcher would miss real evidence.
    plain = walk([obs("/login::login+s1", url="http://x/login")])
    wall = walk([obs("/login::login+s1", url="http://x/login?next=%2Fmy-list")])
    merged = sa.merge_walks({"/": plain, "/my-list": wall})
    assert len(merged["observations"]) == 2
    matcher = {"kind": "observation", "route": "/login", "flags": ["login"],
               "url_contains": "next="}
    assert sa.match_screen(matcher, merged) == "/login::login+s1"


def test_login_roundtrip_must_hold_within_a_single_walk():
    # Walk A carries the next= param but never reaches the target; walk B
    # reaches the target but has no login wall. Neither walk alone satisfies
    # the round-trip, and merging must not stitch them into a fake pass.
    walk_a = _roundtrip_walk("http://x/login?next=%2Fbrowse", "http://x/profiles",
                             downstream_ok=False)
    walk_b = walk(
        [obs("/profiles::chooser+s2", url="http://x/profiles"), obs("/browse::s3")],
        [tr("/profiles::chooser+s2", "/browse::s3", "select first option 'p'")],
    )
    manifest = {
        "screens": [],
        "transitions": [{"id": "t-rt", "from": "login", "to": "browse", "roles": ["member"]}],
        "mustDiscover": [{"id": "t-rt", "kind": "transition", "why": "round-trip"}],
    }
    config = {
        "roles": {"member": {"threshold": 0.8}},
        "screen_matchers": {},
        "transition_matchers": {"t-rt": MATCHER},
    }
    results = sa.evaluate_must_discover(manifest, config, {"member": {"/": walk_a, "/b": walk_b}})
    assert results[0]["found"] is False
    # The same evidence inside ONE walk passes.
    whole = _roundtrip_walk("http://x/login?next=%2Fbrowse", "http://x/profiles")
    results = sa.evaluate_must_discover(manifest, config, {"member": {"/": whole}})
    assert results[0]["found"] is True


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
    verdict = sa.judge(
        MANIFEST, CONFIG, {"guest": {"/": guest}, "member": {"/": member}}, member, {}
    )
    assert verdict["passed"] is True


def test_judge_fails_on_walk_crash():
    member = _full_member_walk()
    guest = walk([obs("/a::s1")])
    verdict = sa.judge(
        MANIFEST, CONFIG, {"guest": {"/": guest}, "member": {"/": member}}, member,
        {"premium@/": "RuntimeError: boom"},
    )
    assert verdict["passed"] is False


def test_judge_fails_on_determinism_mismatch():
    member = _full_member_walk()
    guest = walk([obs("/a::s1")])
    rewalk = walk([obs(f"/{s}::s1") for s in "abcd"], [tr("/a::s1", "/b::s2", "scroll feed")])
    verdict = sa.judge(
        MANIFEST, CONFIG, {"guest": {"/": guest}, "member": {"/": member}}, rewalk, {}
    )
    assert verdict["determinism"]["passed"] is False
    assert verdict["passed"] is False


def test_judge_compares_determinism_against_primary_entry_path_walk():
    # The determinism re-walk matches the PRIMARY ("/" first-listed) walk, not
    # the secondary entry path's walk.
    config = {**CONFIG, "roles": {**CONFIG["roles"],
                                  "member": {**CONFIG["roles"]["member"],
                                             "entry_paths": ["/", "/b"]}}}
    primary = _full_member_walk()
    secondary = walk([obs("/b::s2")])
    guest = walk([obs("/a::s1")])
    verdict = sa.judge(
        MANIFEST, config,
        {"guest": {"/": guest}, "member": {"/": primary, "/b": secondary}},
        primary, {},
    )
    assert verdict["determinism_path"] == "/"
    assert verdict["determinism"]["passed"] is True
    # Re-walk equal to the SECONDARY walk must fail the gate.
    verdict = sa.judge(
        MANIFEST, config,
        {"guest": {"/": guest}, "member": {"/": primary, "/b": secondary}},
        secondary, {},
    )
    assert verdict["determinism"]["passed"] is False


def test_validate_config_rejects_matcher_gaps():
    incomplete = {**CONFIG, "screen_matchers": {"a": {"route": "/a"}}}
    with pytest.raises(SystemExit):
        sa.validate_config(MANIFEST, incomplete)
    no_transition = {**CONFIG, "transition_matchers": {}}
    with pytest.raises(SystemExit):
        sa.validate_config(MANIFEST, no_transition)


def test_validate_config_rejects_malformed_entry_paths():
    for bad in ([], "/a", ["relative"], [1]):
        broken = {**CONFIG, "roles": {**CONFIG["roles"],
                                      "member": {**CONFIG["roles"]["member"],
                                                 "entry_paths": bad}}}
        with pytest.raises(SystemExit):
            sa.validate_config(MANIFEST, broken)


def test_validate_config_accepts_depth_override_entries():
    config = {**CONFIG, "roles": {**CONFIG["roles"],
                                  "member": {**CONFIG["roles"]["member"],
                                             "entry_paths": [
                                                 "/", {"path": "/a", "depth": 5},
                                                 {"path": "/b"}]}}}
    sa.validate_config(MANIFEST, config)  # must not raise


def test_validate_config_rejects_malformed_depth_override_entries():
    for bad_entry in (
        {"path": "relative", "depth": 5},   # path must be '/'-prefixed
        {"depth": 5},                       # path missing
        {"path": "/a", "depth": 0},         # depth must be >= 1
        {"path": "/a", "depth": -3},
        {"path": "/a", "depth": "deep"},    # depth must be an int
        {"path": "/a", "depth": True},      # bools are not depths
        {"path": 1, "depth": 5},
    ):
        broken = {**CONFIG, "roles": {**CONFIG["roles"],
                                      "member": {**CONFIG["roles"]["member"],
                                                 "entry_paths": ["/", bad_entry]}}}
        with pytest.raises(SystemExit):
            sa.validate_config(MANIFEST, broken)


def _config_with_screen_matcher(matcher):
    return {**CONFIG, "screen_matchers": {**CONFIG["screen_matchers"], "a": matcher}}


def test_validate_config_rejects_empty_screen_matcher():
    # An empty matcher matches the FIRST observation of any walk.
    with pytest.raises(SystemExit, match="positive predicate"):
        sa.validate_config(MANIFEST, _config_with_screen_matcher({}))


def test_validate_config_rejects_absence_only_screen_matcher():
    for matcher in (
        {"flags_absent": ["dialog"]},
        {"testid_absent": "end-of-catalog"},
        {"flags_absent": ["dialog"], "testid_absent": "end-of-catalog"},
        {"flags": [], "flags_absent": ["dialog"]},  # empty flags list is not positive
    ):
        with pytest.raises(SystemExit, match="positive predicate"):
            sa.validate_config(MANIFEST, _config_with_screen_matcher(matcher))


def test_validate_config_accepts_route_plus_absence_matcher():
    # browse-eager style: route is the positive anchor, absences refine it.
    sa.validate_config(MANIFEST, _config_with_screen_matcher(
        {"route": "/a", "flags_absent": ["exhausted", "dialog"],
         "testid_absent": "end-of-catalog"}))


def test_validate_config_rejects_scroll_growth_without_to_evidence():
    # Without to_* evidence the matcher passes on ANY digest change.
    broken = {**CONFIG, "transition_matchers": {
        "t-scroll": {"kind": "scroll-growth", "trigger": "scroll feed", "route": "/a"}}}
    with pytest.raises(SystemExit, match="scroll-growth"):
        sa.validate_config(MANIFEST, broken)
    empty_flags = {**CONFIG, "transition_matchers": {
        "t-scroll": {"kind": "scroll-growth", "trigger": "scroll feed", "route": "/a",
                     "to_flags": []}}}
    with pytest.raises(SystemExit, match="scroll-growth"):
        sa.validate_config(MANIFEST, empty_flags)


def test_validate_config_rejects_duplicate_entry_paths_within_role():
    for dup in (
        ["/", "/"],
        ["/a", {"path": "/a", "depth": 2}],
        ["/a", "/a/"],  # normalized: trailing slash is the same walk key
    ):
        broken = {**CONFIG, "roles": {**CONFIG["roles"],
                                      "member": {**CONFIG["roles"]["member"],
                                                 "entry_paths": dup}}}
        with pytest.raises(SystemExit, match="duplicate entry path"):
            sa.validate_config(MANIFEST, broken)


def test_validate_config_allows_same_entry_path_across_roles():
    config = {**CONFIG, "roles": {
        "guest": {**CONFIG["roles"]["guest"], "entry_paths": ["/", "/a"]},
        "member": {**CONFIG["roles"]["member"], "entry_paths": ["/", "/a"]},
    }}
    sa.validate_config(MANIFEST, config)  # must not raise


# --------------------------------------------------------------------------
# Parallel-walk plumbing: plan-order output and crash propagation
# --------------------------------------------------------------------------


def _task(label, role="member", entry_path="/", depth=6):
    return {"label": label, "role": role, "entry_path": entry_path, "depth": depth,
            "port": 0, "creds": {}, "image": None, "server_cmd": None}


def _result(label, walk_dict=None, error=None, seconds=1.0):
    return {"label": label, "walk": walk_dict, "error": error, "seconds": seconds}


def test_order_results_restores_plan_order_regardless_of_completion_order():
    tasks = [_task("a@/"), _task("b@/"), _task("c@/")]
    # Completion order c, a, b — the output must follow the plan: a, b, c.
    by_label = {}
    for label in ("c@/", "a@/", "b@/"):
        by_label[label] = _result(label)
    assert [r["label"] for r in sa.order_results(tasks, by_label)] == ["a@/", "b@/", "c@/"]


def test_assemble_results_groups_walks_and_keeps_plan_order():
    tasks = [
        _task("member@/", "member", "/"),
        _task("member@/a", "member", "/a"),
        _task("member-determinism@/", "member", "/"),
    ]
    w1, w2, w3 = walk([obs("/a::s1")]), walk([obs("/b::s2")]), walk([obs("/a::s1")])
    results = [_result(t["label"], w) for t, w in zip(tasks, (w1, w2, w3))]
    walks, rewalk, crashes, timings, labelled = sa.assemble_results(
        tasks, results, "member-determinism@/"
    )
    assert walks == {"member": {"/": w1, "/a": w2}}
    assert rewalk is w3  # the determinism re-walk is kept out of the role walks
    assert crashes == {}
    assert list(timings) == ["member@/", "member@/a", "member-determinism@/"]
    assert [(label, role, path) for label, role, path, _ in labelled] == [
        ("member@/", "member", "/"),
        ("member@/a", "member", "/a"),
        ("member-determinism@/", "member", "/"),
    ]


def test_assemble_results_crash_is_visible_and_fails_judgement():
    tasks = [_task("member@/", "member", "/"), _task("guest@/", "guest", "/")]
    results = [
        _result("member@/", _full_member_walk()),
        _result("guest@/", None, error="RuntimeError: sandbox never came up"),
    ]
    walks, rewalk, crashes, timings, labelled = sa.assemble_results(tasks, results, "x")
    assert crashes == {"guest@/": "RuntimeError: sandbox never came up"}
    assert "guest" not in walks  # crashed walk contributes no observations
    assert len(labelled) == 1
    verdict = sa.judge(MANIFEST, CONFIG, walks, walks["member"]["/"], crashes)
    assert verdict["passed"] is False  # crash gate
    assert "guest@/" in verdict["crashes"]


def test_execute_walk_task_reports_crash_instead_of_raising():
    # A task whose sandbox command can never become healthy must come back as
    # an error result, not an exception (the parent runs this in a pool).
    task = _task("guest@/")
    task["server_cmd"] = "false"  # exits immediately; /healthz never answers
    import unittest.mock

    with unittest.mock.patch.object(sa, "_wait_healthz",
                                    side_effect=RuntimeError("not healthy")):
        result = sa.execute_walk_task(task)
    assert result["label"] == "guest@/"
    assert result["walk"] is None
    assert "not healthy" in result["error"]


def test_preassign_ports_are_distinct():
    ports = sa._preassign_ports(8)
    assert len(set(ports)) == 8


def test_boot_with_retry_recovers_on_a_fresh_port(capsys):
    # Preassigned port taken twice; the third attempt (second fresh ephemeral
    # port) succeeds. Retries are logged to stderr.
    booted_ports = []

    def boot(label, port=None):
        booted_ports.append(port)
        if len(booted_ports) < 3:
            raise RuntimeError(f"address already in use: {port}")
        return f"http://127.0.0.1:{port}"

    fresh = iter([5001, 5002])
    base_url = sa.boot_with_retry(boot, "member@/", 4000,
                                  attempts=3, fresh_port=lambda: next(fresh))
    assert base_url == "http://127.0.0.1:5002"
    assert booted_ports == [4000, 5001, 5002]
    err = capsys.readouterr().err
    assert err.count("retrying on a fresh ephemeral port") == 2


def test_boot_with_retry_gives_up_after_bounded_attempts():
    booted_ports = []

    def boot(label, port=None):
        booted_ports.append(port)
        raise RuntimeError("address already in use")

    with pytest.raises(RuntimeError, match="address already in use"):
        sa.boot_with_retry(boot, "member@/", 4000, attempts=3, fresh_port=lambda: 9999)
    assert len(booted_ports) == 3  # bounded: never loops forever


def test_normalize_walk_urls_strips_origin_keeps_path_query_fragment():
    w = walk([
        obs("/login::login+s1", url="http://127.0.0.1:63684/login?next=%2Fbrowse"),
        obs("/a::s2", url="http://127.0.0.1:63684/a#frag"),
        obs("/ext::s3", url="https://elsewhere.example/x"),  # foreign origin untouched
    ])
    out = sa.normalize_walk_urls(w, "http://127.0.0.1:63684")
    urls = [o["url"] for o in out["observations"]]
    assert urls == [
        "http://sandbox/login?next=%2Fbrowse",
        "http://sandbox/a#frag",
        "https://elsewhere.example/x",
    ]


def test_normalized_walks_are_port_independent_and_raw_jsonl_is_port_free(tmp_path):
    # The same walk observed on two different ports must normalize to the same
    # bytes — a boot retry landing on another port cannot break determinism.
    def walk_on(port):
        return sa.normalize_walk_urls(
            walk([obs("/a::s1", url=f"http://127.0.0.1:{port}/a?x=1")]),
            f"http://127.0.0.1:{port}",
        )

    first, second = walk_on(4001), walk_on(59999)
    assert first == second
    out = tmp_path / "raw.jsonl"
    sa.write_raw_jsonl(out, {"schema": 2}, [("member@/", "member", "/", first)])
    text = out.read_text(encoding="utf-8")
    assert "4001" not in text and "127.0.0.1" not in text


def test_jobs_equivalence_sequential_vs_parallel_byte_identical(tmp_path):
    # The same task plan through sequential (jobs=1) and parallel collection,
    # with a fake execute that completes out of plan order, must yield the
    # same results, the same assembled judge inputs, the same verdict, and
    # byte-identical raw.jsonl.
    import copy
    import time as time_mod
    from concurrent.futures import ThreadPoolExecutor

    tasks = [
        _task("guest@/", "guest", "/"),
        _task("member@/", "member", "/"),
        _task("member@/a", "member", "/a"),
        _task("member-determinism@/", "member", "/"),
    ]
    walks_by_label = {
        "guest@/": walk([obs("/a::s1")]),
        "member@/": _full_member_walk(),
        "member@/a": walk([obs("/b::s2", visible_copy=["Lazy row"])]),
        "member-determinism@/": _full_member_walk(),
    }
    # Earlier plan entries take longer: completion order is the REVERSE of
    # plan order in the parallel run.
    delays = {"guest@/": 0.15, "member@/": 0.1, "member@/a": 0.05,
              "member-determinism@/": 0.0}

    def fake_execute(task):
        time_mod.sleep(delays[task["label"]])
        return _result(task["label"], copy.deepcopy(walks_by_label[task["label"]]),
                       seconds=1.0)

    sequential = sa.run_plan(tasks, 1, execute=fake_execute)
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        parallel = sa.collect_parallel(tasks, pool, fake_execute)

    assert [r["label"] for r in sequential] == [t["label"] for t in tasks]
    assert sequential == parallel

    raw_bytes = {}
    verdicts = {}
    for name, results in (("sequential", sequential), ("parallel", parallel)):
        walks_by_role, rewalk, crashes, _timings, labelled = sa.assemble_results(
            tasks, results, "member-determinism@/"
        )
        out = tmp_path / f"{name}.jsonl"
        sa.write_raw_jsonl(out, {"schema": 2}, labelled)
        raw_bytes[name] = out.read_bytes()
        verdicts[name] = sa.judge(MANIFEST, CONFIG, walks_by_role, rewalk, crashes)
    assert raw_bytes["sequential"] == raw_bytes["parallel"]
    assert verdicts["sequential"] == verdicts["parallel"]
    assert verdicts["sequential"]["passed"] is True


# --------------------------------------------------------------------------
# The shipped judge config stays in lockstep with the shipped manifest schema
# --------------------------------------------------------------------------


def test_shipped_config_parses_and_has_known_matcher_kinds():
    import json

    config_path = Path(__file__).resolve().parent.parent / "acceptance" / "streamly-sandbox.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(config["roles"]) == {"guest", "member", "premium"}
    for role, spec in config["roles"].items():
        paths = sa.role_entry_paths(config, role)
        assert paths and all(p.startswith("/") for p in paths)
        assert paths[0] == "/", "every role's primary entry path is the app root"
    for matcher in config["screen_matchers"].values():
        assert matcher.get("kind", "observation") == "observation"
        assert any(matcher.get(k) for k in sa._POSITIVE_OBS_PREDICATES), \
            "every shipped screen matcher carries a positive predicate"
    for matcher in config["transition_matchers"].values():
        assert matcher["kind"] in {"transition", "login-roundtrip", "scroll-growth"}
        if matcher["kind"] == "scroll-growth":
            assert any(matcher.get(k) for k in sa._TO_EVIDENCE_KEYS), \
                "shipped scroll-growth matchers carry to-observation evidence"


def test_shipped_config_entry_depths():
    import json

    config_path = Path(__file__).resolve().parent.parent / "acceptance" / "streamly-sandbox.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    # Primary walks keep the default depth 6; secondary entries carry the
    # verified trims (login wall is hop 0 of /my-list; plan page hop 2 and
    # upgrade-confirm hop 3 of /account/plan).
    assert sa.role_entry_specs(config, "guest") == [("/", 6), ("/my-list", 2)]
    assert sa.role_entry_specs(config, "member") == [("/", 6), ("/account/plan", 5)]
    assert sa.role_entry_specs(config, "premium") == [("/", 6), ("/account/plan", 5)]


# --------------------------------------------------------------------------
# Pulled credential overlay (PULL model, sidira-simulator #206 Slice C)
# --------------------------------------------------------------------------


def _cred_config():
    """Minimal judge config carrying the role shape apply_pulled_credentials
    overlays onto — guest has no login, member/premium do."""
    return {
        "roles": {
            "guest": {"username": None, "password": None},
            "member": {"username": "config-member@x.test", "password": "config-mbr-pw"},
            "premium": {"username": "config-premium@x.test", "password": "config-prm-pw"},
        }
    }


def _creds_env(*logins):
    return json.dumps({"customer_id": "c", "logins": list(logins)})


def test_pulled_credentials_overlay_overrides_config(monkeypatch):
    config = _cred_config()
    monkeypatch.setenv(
        sa._PULLED_CREDENTIALS_ENV,
        _creds_env(
            {"role": "member", "username": "real-member@x.test", "password": "real-mbr-pw"},
            {"role": "premium", "username": "real-premium@x.test", "password": "real-prm-pw"},
        ),
    )
    applied = sa.apply_pulled_credentials(config)
    assert applied == ["member", "premium"]
    assert config["roles"]["member"] == {
        "username": "real-member@x.test",
        "password": "real-mbr-pw",
    }
    assert config["roles"]["premium"]["password"] == "real-prm-pw"
    # The no-login role is untouched.
    assert config["roles"]["guest"] == {"username": None, "password": None}


def test_pulled_credentials_absent_is_noop(monkeypatch):
    monkeypatch.delenv(sa._PULLED_CREDENTIALS_ENV, raising=False)
    config = _cred_config()
    before = json.loads(json.dumps(config))
    assert sa.apply_pulled_credentials(config) == []
    assert config == before


def test_pulled_credentials_blank_env_is_noop(monkeypatch):
    monkeypatch.setenv(sa._PULLED_CREDENTIALS_ENV, "   ")
    config = _cred_config()
    assert sa.apply_pulled_credentials(config) == []
    assert config["roles"]["member"]["password"] == "config-mbr-pw"


def test_pulled_credentials_extra_role_is_ignored(monkeypatch):
    config = _cred_config()
    # Covers both config login roles PLUS an extra role this config doesn't walk;
    # the extra is ignored, the known ones apply, and completeness holds.
    monkeypatch.setenv(
        sa._PULLED_CREDENTIALS_ENV,
        _creds_env(
            {"role": "member", "username": "real-member@x.test", "password": "real-mbr-pw"},
            {"role": "premium", "username": "real-premium@x.test", "password": "real-prm-pw"},
            {"role": "admin", "username": "a@x.test", "password": "adm-pw"},
        ),
    )
    assert sa.apply_pulled_credentials(config) == ["member", "premium"]
    assert "admin" not in config["roles"]
    assert config["roles"]["member"]["password"] == "real-mbr-pw"


def test_pulled_credentials_never_authenticates_a_no_login_role(monkeypatch):
    """A pulled login for a config no-login role (guest) must be ignored — the
    config decides which roles authenticate, the pull only supplies secrets
    (codex P2)."""
    config = _cred_config()
    monkeypatch.setenv(
        sa._PULLED_CREDENTIALS_ENV,
        _creds_env(
            {"role": "member", "username": "real-member@x.test", "password": "real-mbr-pw"},
            {"role": "premium", "username": "real-premium@x.test", "password": "real-prm-pw"},
            {"role": "guest", "username": "sneaky@x.test", "password": "sneaky-pw"},
        ),
    )
    assert sa.apply_pulled_credentials(config) == ["member", "premium"]
    # guest stays anonymous despite a pulled login.
    assert config["roles"]["guest"] == {"username": None, "password": None}


def test_pulled_credentials_partial_pull_hard_fails(monkeypatch):
    """A successful pull that omits a configured login role must fail, not crawl
    that role with stale checked-in creds (codex P2)."""
    config = _cred_config()  # login roles: member, premium
    monkeypatch.setenv(
        sa._PULLED_CREDENTIALS_ENV,
        _creds_env({"role": "member", "username": "real-member@x.test", "password": "real-mbr-pw"}),
    )
    with pytest.raises(SystemExit) as exc:
        sa.apply_pulled_credentials(config)
    assert "premium" in str(exc.value)


def test_pulled_credentials_malformed_json_raises(monkeypatch):
    monkeypatch.setenv(sa._PULLED_CREDENTIALS_ENV, "{not json")
    with pytest.raises(SystemExit):
        sa.apply_pulled_credentials(_cred_config())


def test_pulled_credentials_missing_logins_key_raises(monkeypatch):
    monkeypatch.setenv(sa._PULLED_CREDENTIALS_ENV, json.dumps({"customer_id": "c"}))
    with pytest.raises(SystemExit):
        sa.apply_pulled_credentials(_cred_config())
