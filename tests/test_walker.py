"""Walker integration tests against the hermetic hostile fixture app.

The fixture app ("Plateful", a recipe site) is intentionally neutral content
served from tests/fixture_app by a plain http.server. It exercises every
hostile trait the walker must handle generically: JS-only shell, blocking
consent overlay, login wall with ?next= round-trip, chooser interstitial,
lazy/bounded infinite feed with a terminal marker, modal-with-URL, and a
transient toast.
"""

from __future__ import annotations

import time

import pytest

from sidira_crawler.walker import (
    _PENDING_REQUESTS,
    _SETTLE_STABLE_DESPITE_PENDING_ROUNDS,
    SCROLL_HARD_CAP,
    RawWalk,
    _execute_action,
    _goto,
    _scroll_page,
    _settle,
    _track_network,
    walk,
)

pytestmark = pytest.mark.walker

# Credentials accepted by the fixture app's client-side login form.
FIXTURE_USER = "cook@example.test"
FIXTURE_PASSWORD = "sourdough"


@pytest.fixture(scope="session")
def full_walk(fixture_server, browser) -> RawWalk:
    """One full credentialed walk shared by most assertions."""
    return walk(
        fixture_server + "/",
        "member",
        username=FIXTURE_USER,
        password=FIXTURE_PASSWORD,
        depth=6,
        browser=browser,
    )


def _screens(walk_result: RawWalk) -> dict[str, object]:
    return {o.screen_id: o for o in walk_result.observations}


def _routes(walk_result: RawWalk) -> set[str]:
    return {o.route for o in walk_result.observations}


# ---------------------------------------------------------------------------
# Consent overlay
# ---------------------------------------------------------------------------


def test_consent_overlay_observed_and_dismissed(full_walk: RawWalk) -> None:
    overlay_screens = [o for o in full_walk.observations if o.has_blocking_overlay]
    assert overlay_screens, "pre-dismissal overlay state must be its own screen"
    overlay = overlay_screens[0]
    assert overlay.route == "/"

    clean_home = [
        o for o in full_walk.observations if o.route == "/" and not o.has_blocking_overlay
    ]
    assert clean_home, "the page behind the overlay must be observed too"

    dismissals = [
        t
        for t in full_walk.transitions
        if t.from_screen_id == overlay.screen_id and "consent" in t.trigger.lower()
    ]
    assert dismissals, "overlay dismissal must be recorded as a transition"


def test_content_behind_consent_discovered(full_walk: RawWalk) -> None:
    assert "/feed.html" in _routes(full_walk)
    assert "/about.html" in _routes(full_walk)


# ---------------------------------------------------------------------------
# Login wall
# ---------------------------------------------------------------------------


def test_login_roundtrip_with_credentials(full_walk: RawWalk) -> None:
    screens = _screens(full_walk)
    login_ids = [s for s, o in screens.items() if o.route == "/login.html"]
    assert login_ids, "guarded URL must bounce to an observed login screen"
    login_id = login_ids[0]

    # Guard: some click landed on the login screen (the ?next= bounce).
    into_login = [t for t in full_walk.transitions if t.to_screen_id == login_id]
    assert into_login, "the guard -> login bounce must be a transition"

    # Auth: submitting the form is one hop out of the login screen.
    submits = [
        t
        for t in full_walk.transitions
        if t.from_screen_id == login_id and t.trigger == "submit login form"
    ]
    assert submits, "login submission must be recorded as a transition"

    # Round-trip: the originally-guarded target is eventually reached.
    members = [s for s, o in screens.items() if o.route == "/members.html"]
    assert members, "after auth the app must return to the guarded target"

    # The chain connects: login -> (chooser) -> members.
    after_login = submits[0].to_screen_id
    reachable = {after_login}
    frontier = [after_login]
    while frontier:
        cur = frontier.pop()
        for t in full_walk.transitions:
            if t.from_screen_id == cur and t.to_screen_id not in reachable:
                reachable.add(t.to_screen_id)
                frontier.append(t.to_screen_id)
    assert members[0] in reachable, "guard -> login -> ... -> target chain must connect"


def test_second_guarded_target_reached(full_walk: RawWalk) -> None:
    """Two guarded pages bounce to the SAME login screen with DIFFERENT ?next=
    continuations. Observation dedupe must not eat the second round-trip."""
    routes = _routes(full_walk)
    assert "/members.html" in routes, "first guarded target must be reached"
    assert "/billing.html" in routes, (
        "second guarded target must be reached too — its login bounce shares the "
        "login screen but carries a different ?next= continuation"
    )


def test_login_wall_without_credentials(fixture_server, browser) -> None:
    result = walk(fixture_server + "/", "guest", depth=3, browser=browser)
    routes = _routes(result)
    assert "/login.html" in routes, "login screen must still be observed without creds"
    assert "/members.html" not in routes, "the guard must hold without credentials"


# ---------------------------------------------------------------------------
# Chooser interstitial
# ---------------------------------------------------------------------------


def test_interstitial_chooser(full_walk: RawWalk) -> None:
    screens = _screens(full_walk)
    chooser_ids = [s for s in screens if "chooser" in s]
    assert chooser_ids, "the profile-picker interstitial must be observed as a screen"
    chooser_id = chooser_ids[0]

    onward = [
        t
        for t in full_walk.transitions
        if t.from_screen_id == chooser_id and t.trigger.startswith("select first option")
    ]
    assert onward, "the walk must proceed past the interstitial via its first option"
    assert screens[onward[0].to_screen_id].route == "/members.html"


def test_chooser_replay_is_idempotent_when_already_authenticated(
    fixture_server, browser
) -> None:
    """Regression for sidira-crawler#8: replaying a path through a login +
    profile-chooser interstitial inside an ALREADY-authenticated context must
    land back on the target, not misclick the chooser step off-target.

    A node behind the login wall records [click -> wall, login -> chooser,
    choose_first -> target]. The walker restores that node between candidate
    expansions by replaying the path. But the context is authenticated by then,
    so members.html does NOT bounce: the login submit is a no-op and the app
    never shows the profile picker. The replayed choose_first step therefore
    finds no chooser. The old code blindly clicked candidate index 0 (here a
    nav link), navigating to '/', failing _state_matches, and abandoning the
    node (walker.restore_failed) — so every state behind it went undiscovered.
    The fix skips choose_first when no genuine chooser group is present."""
    context = browser.new_context()
    try:
        # Pre-authenticate exactly as the login + chooser flow would have, so the
        # target renders directly without the interstitial — the replay context.
        context.add_cookies(
            [
                {"name": "plateful_auth", "value": "1", "url": fixture_server},
                {"name": "plateful_profile", "value": "alex", "url": fixture_server},
                {"name": "plateful_consent", "value": "1", "url": fixture_server},
            ]
        )
        page = context.new_page()
        target = fixture_server + "/members.html"

        # Replay the interstitial tail of the node's path on the authed target.
        _goto(page, target)
        _settle(page)
        _execute_action(page, {"kind": "login"}, FIXTURE_USER, FIXTURE_PASSWORD)
        _execute_action(page, {"kind": "choose_first"}, FIXTURE_USER, FIXTURE_PASSWORD)

        assert page.url == target, (
            "replaying login + choose_first in an authenticated context must stay "
            f"on the target (got {page.url}) — a blind index-0 chooser click "
            "navigates off-target and abandons the node (#8)"
        )
    finally:
        context.close()


# ---------------------------------------------------------------------------
# Same-route wizard
# ---------------------------------------------------------------------------


def test_wizard_steps_on_same_route_are_distinct_screens(full_walk: RawWalk) -> None:
    """A wizard swaps its fields in place: same URL, no flag changes. Each
    step must still be its own screen and the step transition a recorded hop."""
    screens = _screens(full_walk)
    plan_ids = [s for s, o in screens.items() if o.route == "/plan.html"]
    assert len(plan_ids) >= 2, "wizard step 2 must be distinct from step 1"

    step1 = [s for s in plan_ids if "wizard-dish" in screens[s].data_testids]
    step2 = [s for s in plan_ids if "wizard-servings" in screens[s].data_testids]
    assert step1 and step2, "both wizard steps must be observed"

    hops = [
        t
        for t in full_walk.transitions
        if t.from_screen_id in step1 and t.to_screen_id in step2
    ]
    assert hops, "the step-1 -> step-2 transition must be recorded as one hop"


def test_required_inputs_filled_before_submit(full_walk: RawWalk) -> None:
    """Wizard step 1 REJECTS empty submits (required dish text + email). The
    walker must deterministically fill empty text-like inputs before clicking
    button candidates, or advancement is silently blocked and step 2 is
    unreachable for the whole walk."""
    screens = _screens(full_walk)
    step2 = [s for s, o in screens.items() if "wizard-servings" in o.data_testids]
    assert step2, (
        "step 2 is gated behind required step-1 inputs — the walker must fill "
        "them before clicking Next"
    )
    hops = [
        t
        for t in full_walk.transitions
        if t.to_screen_id in step2 and t.trigger == "click button 'Next'"
    ]
    assert hops, "advancing the gated wizard must be recorded as the Next click"


# ---------------------------------------------------------------------------
# Modal with URL
# ---------------------------------------------------------------------------


def test_modal_with_url_is_a_new_screen_one_hop(full_walk: RawWalk) -> None:
    modal_obs = [o for o in full_walk.observations if o.has_dialog]
    assert modal_obs, "the recipe modal must be observed as its own screen"
    modal = modal_obs[0]
    assert modal.route == "/feed.html"
    assert "?recipe=" in modal.url, "the modal action changed the URL without navigation"

    feed_ids = {o.screen_id for o in full_walk.observations if o.route == "/feed.html"}
    assert modal.screen_id in feed_ids and len(feed_ids) >= 2, (
        "the modal must be distinct from the plain feed screen"
    )

    hops_in = [t for t in full_walk.transitions if t.to_screen_id == modal.screen_id]
    assert hops_in, "opening the modal must be exactly one recorded hop"
    assert all(t.from_screen_id != modal.screen_id for t in hops_in)


# ---------------------------------------------------------------------------
# Click integrity: stale indices and deferred async effects
# ---------------------------------------------------------------------------


def test_stale_candidate_index_resolved_by_signature(full_walk: RawWalk) -> None:
    """promo.html's dismissible strip is removed IN PLACE by a preventDefault'd
    link without changing anything the structural digest sees. Every later
    candidate index then points one element off: a bare-index click on
    'Upgrade plan' actually hits 'Sign out' and the recorded transition lands
    on home instead of the in-place confirm panel. The recorded transition's
    trigger must match what its destination implies."""
    screens = _screens(full_walk)
    upgrades = [
        t for t in full_walk.transitions if t.trigger == "click button 'Upgrade plan'"
    ]
    assert upgrades, "the upgrade candidate must produce a recorded transition"
    for hop in upgrades:
        dest = screens.get(hop.to_screen_id)
        assert dest is not None and "upgrade-confirm" in dest.data_testids, (
            f"'Upgrade plan' must land on the confirm panel, not {hop.to_screen_id} — "
            "a stale candidate index hit the wrong element"
        )


def test_deferred_async_click_effect_attributed(full_walk: RawWalk) -> None:
    """sync.html's button mutates the DOM only after a SLOW fetch resolves. A
    settle that ignores in-flight requests classifies the click as a no-op:
    the effect goes unrecorded (or leaks into the next candidate's
    transition). The walker must wait for network quiescence and attribute
    the resulting screen to the click that caused it."""
    synced = [o for o in full_walk.observations if "sync-done" in o.data_testids]
    assert synced, "the deferred effect of the sync click must be observed as a screen"
    hops = [t for t in full_walk.transitions if t.to_screen_id == synced[0].screen_id]
    assert hops, "reaching the synced state must be a recorded transition"
    assert all(t.trigger == "click button 'Sync pantry'" for t in hops), (
        "the synced state must be attributed to the sync click, not a later candidate"
    )


# ---------------------------------------------------------------------------
# Lazy + bounded feed
# ---------------------------------------------------------------------------


def test_feed_exhausted_state_discovered(full_walk: RawWalk) -> None:
    exhausted = [o for o in full_walk.observations if "exhausted" in o.screen_id]
    assert exhausted, "the feed-exhausted state must be observed as its own screen"
    assert "feed-finished" in exhausted[0].data_testids, (
        "the terminal marker must be visible in the exhausted observation"
    )
    scrolls = [t for t in full_walk.transitions if t.trigger == "scroll feed"]
    assert scrolls, "scrolling the feed to exhaustion is a recorded transition"


def test_feed_scroll_terminates_by_structure(fixture_server, browser) -> None:
    """Scrolling stops because content stops growing — never via the hard cap."""
    context = browser.new_context()
    try:
        context.add_cookies(
            [{"name": "plateful_consent", "value": "1", "url": fixture_server}]
        )
        page = context.new_page()
        page.goto(fixture_server + "/feed.html")
        _settle(page)
        exhausted, rounds = _scroll_page(page)
        assert exhausted is True
        assert rounds < SCROLL_HARD_CAP, "termination must come from structure, not budget"
        assert page.locator("[data-testid=feed-finished]").is_visible()
    finally:
        context.close()


# ---------------------------------------------------------------------------
# Settle on never-idle vs slow-render pages (#28)
# ---------------------------------------------------------------------------


def _consented_page(fixture_server, browser):
    context = browser.new_context()
    context.add_cookies([{"name": "plateful_consent", "value": "1", "url": fixture_server}])
    return context, context.new_page()


def test_settle_fast_on_never_idle_page(fixture_server, browser) -> None:
    """A page whose content stabilises instantly but holds a long-poll open
    forever must NOT cost the full round budget. The old settle keyed solely on
    an empty pending set, so a never-idle page (long-poll / SSE / heartbeat —
    most real apps) burned every round on every call. _settle now returns on a
    DOM that is byte-stable despite the in-flight request, bounded well under
    the old worst case."""
    context, page = _consented_page(fixture_server, browser)
    try:
        # Attach request tracking before navigating, exactly as _goto does, so
        # the long-poll fired on load is counted (settle's own _track_network
        # would otherwise miss a request that started before it ran).
        _track_network(page)
        page.goto(fixture_server + "/neveridle.html")
        start = time.monotonic()
        _settle(page)
        elapsed = time.monotonic() - start

        # The fixture really is never-idle: the long-poll is still in flight at
        # return, so settle could only have come back via the stable-despite-
        # pending path — not the empty-pending fast path.
        assert _PENDING_REQUESTS.get(page), "the long-poll request must still be pending"
        # Bounded: ~_SETTLE_STABLE_DESPITE_PENDING_ROUNDS rounds (~3.0s), far
        # under the old ~6.8s (2s networkidle + 60×80ms). Generous ceiling to
        # stay non-flaky on a loaded CI box.
        assert elapsed < 4.5, f"never-idle settle took {elapsed:.2f}s — budget not bounded"
        # The content that DID render is intact.
        assert page.locator("[data-testid=shelf-ready]").is_visible()
    finally:
        context.close()


def test_settle_preserves_slow_render_recall(fixture_server, browser) -> None:
    """A page that streams its content in over ~1s (a chunk fetch in flight the
    whole time) must settle only once the DOM stops growing — never mid-render.
    This is the recall guardrail on the stable-despite-pending threshold: were
    it shorter than a chunk's latency, the tail of the list would be lost."""
    context, page = _consented_page(fixture_server, browser)
    try:
        page.goto(fixture_server + "/slowrender.html")
        _settle(page)
        # Every streamed item is present and the terminal marker rendered: the
        # settle waited out the full incremental render.
        assert page.locator("[data-testid=shelf-restocked]").is_visible(), (
            "settle returned before the slow render finished — recall loss"
        )
        items = page.locator("[data-testid=shelf-items] li").count()
        assert items == 8, f"expected all 8 streamed items, got {items}"
    finally:
        context.close()


# Guards the tuning rationale: the threshold must outlast the slowest
# DOM-affecting fetch in the fixture app (the 1.5s deferred sync click), so the
# despite-pending escape can't fire before such an effect lands.
def test_despite_pending_threshold_outlasts_slowest_fetch() -> None:
    assert _SETTLE_STABLE_DESPITE_PENDING_ROUNDS * 0.08 > 1.5


# ---------------------------------------------------------------------------
# Hash routes vs in-page anchors
# ---------------------------------------------------------------------------


def test_hash_route_is_navigation_but_plain_anchor_is_not(full_walk: RawWalk) -> None:
    routes = _routes(full_walk)
    assert "/about.html#/specials" in routes, (
        "hash ROUTES (#/...) are real SPA navigation and must be discovered "
        "as their own route"
    )
    specials = [o for o in full_walk.observations if o.route == "/about.html#/specials"]
    assert "specials-list" in specials[0].data_testids

    hops = [t for t in full_walk.transitions if t.to_screen_id == specials[0].screen_id]
    assert hops, "entering the hash route must be a recorded transition"

    assert all("about-anchor" not in o.url for o in full_walk.observations), (
        "plain in-page #anchors must not be followed or become screens"
    )
    assert all("about-anchor" not in r for r in routes)


# ---------------------------------------------------------------------------
# Depth, determinism, toasts
# ---------------------------------------------------------------------------


def test_depth_budget_respected(fixture_server, browser) -> None:
    result = walk(fixture_server + "/", "guest", depth=1, browser=browser)
    assert _routes(result) == {"/"}, "with depth=1 only 1-hop screens may appear"
    # Entry (overlay) plus its dismissal — nothing deeper.
    assert len(result.observations) == 2


def test_two_identical_walks_are_identical(fixture_server, browser) -> None:
    kwargs = dict(username=FIXTURE_USER, password=FIXTURE_PASSWORD, depth=2, browser=browser)
    first = walk(fixture_server + "/", "member", **kwargs)
    second = walk(fixture_server + "/", "member", **kwargs)
    assert {o.screen_id for o in first.observations} == {
        o.screen_id for o in second.observations
    }
    def as_set(walk_result: RawWalk) -> set[tuple[str, str, str]]:
        return {(t.from_screen_id, t.to_screen_id, t.trigger) for t in walk_result.transitions}

    assert as_set(first) == as_set(second)


def test_toast_is_not_a_screen(full_walk: RawWalk) -> None:
    home_screens = [o for o in full_walk.observations if o.route == "/"]
    assert len(home_screens) == 2, "home must yield exactly overlay + clean screens"
    for obs in full_walk.observations:
        assert "Saved to your list" not in obs.visible_copy, (
            "transient aria-live toasts must not leak into observations"
        )
