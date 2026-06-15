"""Hermetic tests for the second-level clustering pass (capabilities → domains).

No network, no LLM: the LLM is a canned fake. The pass is best-effort, so most
of these assert the degrade-to-empty behaviour that keeps a domain failure from
ever blocking a crawl.
"""

from __future__ import annotations

import json

from sidira_crawler.synthesis.domains import CapabilityBrief, synthesize_domains


class FakeLLM:
    """Canned LLM: returns queued responses, records every prompt."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, max_tokens=4096):
        self.calls.append((system, user))
        return self.responses.pop(0)


class BrokenLLM:
    """An unusable client — stands in for the bare object() the gate tests pass."""

    def complete(self, system, user, max_tokens=4096):
        raise RuntimeError("no LLM configured")


_BRIEFS = [
    CapabilityBrief(name="account", goal="manage your account", surfaces=["/account"]),
    CapabilityBrief(name="account_plan", goal="change plan", surfaces=["/account/plan"]),
    CapabilityBrief(name="browse", goal="find titles", surfaces=["/browse"]),
]

_GROUPED = {
    "domains": [
        {"label": "Account & billing", "capabilities": ["account", "account_plan"]},
        {"label": "Content discovery", "capabilities": ["browse"]},
    ]
}


def test_groups_capabilities_into_domains():
    llm = FakeLLM([json.dumps(_GROUPED)])
    mapping = synthesize_domains(_BRIEFS, llm)
    assert mapping == {
        "account": "Account & billing",
        "account_plan": "Account & billing",
        "browse": "Content discovery",
    }


def test_tolerates_code_fences():
    llm = FakeLLM(["```json\n" + json.dumps(_GROUPED) + "\n```"])
    assert synthesize_domains(_BRIEFS, llm)["browse"] == "Content discovery"


def test_single_capability_skips_the_llm():
    llm = FakeLLM([json.dumps(_GROUPED)])
    assert synthesize_domains(_BRIEFS[:1], llm) == {}
    assert llm.calls == []  # never asked the model


def test_omitted_capability_is_left_ungrouped_not_an_error():
    # The model groups two of three caps; the third simply stays out of the map
    # (consumer renders it "Ungrouped"). One call, no retry.
    partial = {"domains": [{"label": "Account & billing", "capabilities": ["account", "account_plan"]}]}
    llm = FakeLLM([json.dumps(partial)])
    mapping = synthesize_domains(_BRIEFS, llm)
    assert mapping == {"account": "Account & billing", "account_plan": "Account & billing"}
    assert "browse" not in mapping
    assert len(llm.calls) == 1


def test_unknown_slug_retries_then_degrades():
    bad = {"domains": [{"label": "X", "capabilities": ["not_a_real_cap"]}]}
    llm = FakeLLM([json.dumps(bad), json.dumps(bad)])  # retries=1 → 2 attempts
    assert synthesize_domains(_BRIEFS, llm) == {}
    assert len(llm.calls) == 2


def test_duplicate_assignment_retries_then_degrades():
    dup = {
        "domains": [
            {"label": "A", "capabilities": ["account"]},
            {"label": "B", "capabilities": ["account"]},
        ]
    }
    llm = FakeLLM([json.dumps(dup), json.dumps(dup)])
    assert synthesize_domains(_BRIEFS, llm) == {}


def test_garbage_json_degrades_to_empty():
    llm = FakeLLM(["not json at all", "still not json"])
    assert synthesize_domains(_BRIEFS, llm) == {}


def test_malformed_capabilities_list_degrades_without_raising():
    # A null/wrong-typed "capabilities" must be treated as a retryable schema
    # error, never raise out of the best-effort pass and abort the crawl.
    null_caps = {"domains": [{"label": "X", "capabilities": None}]}
    not_a_list = {"domains": {"label": "X", "capabilities": ["account"]}}
    llm = FakeLLM([json.dumps(null_caps), json.dumps(not_a_list)])
    assert synthesize_domains(_BRIEFS, llm) == {}
    assert len(llm.calls) == 2


def test_llm_error_degrades_to_empty_without_raising():
    # The build/publish path must never crash because the domain pass failed.
    assert synthesize_domains(_BRIEFS, BrokenLLM()) == {}


def test_recovers_on_retry_after_a_bad_first_response():
    llm = FakeLLM(["garbage", json.dumps(_GROUPED)])
    mapping = synthesize_domains(_BRIEFS, llm)
    assert mapping["account"] == "Account & billing"
    assert len(llm.calls) == 2
