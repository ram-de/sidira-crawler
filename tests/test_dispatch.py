"""Tests for the dispatch adapter (#23) — simulator config_json → CustomerConfig.

Covers the field mapping, walk_depth threading, credential overlay semantics,
and the `run-dispatch` CLI wiring (without actually walking a sandbox).
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from sidira_crawler import cli as cli_module
from sidira_crawler.cli import cli
from sidira_crawler.dispatch import (
    CREDENTIALS_ENV,
    apply_pulled_credentials,
    build_customer_config,
)
from sidira_crawler.honesty import HonestyGateError, HonestyVerdict


def _payload(**overrides) -> dict:
    """A dispatch payload shaped exactly like CrawlSetup.dispatch_payload()."""
    base = {
        "product_id": "default",
        "sandbox_url": "https://sandbox.example.com",
        "walk_depth": 3,
        "cadence": "weekly",
        "capabilities": [
            {"name": "onboarding", "entry_urls": ["https://sandbox.example.com/welcome"]},
            {"name": "billing", "entry_urls": ["/billing", "/billing/invoices"]},
        ],
        "roles": ["admin", "viewer"],
    }
    base.update(overrides)
    return base


def _creds(*roles: str) -> str:
    return json.dumps(
        {"logins": [{"role": r, "username": f"{r}@x.io", "password": f"pw-{r}"} for r in roles]}
    )


# ── build_customer_config ──────────────────────────────────────────────────


def test_build_maps_dispatch_fields() -> None:
    config = build_customer_config(_payload(), customer_id="org_123")

    assert config.customer_id == "org_123"
    assert config.sandbox_base_url == "https://sandbox.example.com"
    assert [c.name for c in config.capabilities] == ["onboarding", "billing"]
    # entry_urls -> entry_points, verbatim
    assert config.capabilities[1].entry_points == ["/billing", "/billing/invoices"]
    # top-level roles fan out onto every capability
    assert config.capabilities[0].roles == ["admin", "viewer"]
    assert config.capabilities[1].roles == ["admin", "viewer"]


def test_build_threads_walk_depth_into_max_hops() -> None:
    config = build_customer_config(_payload(walk_depth=4), customer_id="org_123")
    assert all(c.max_hops == 4 for c in config.capabilities)


def test_build_defaults_walk_depth_when_absent() -> None:
    payload = _payload()
    del payload["walk_depth"]
    config = build_customer_config(payload, customer_id="org_123")
    # Falls back to CapabilityConfig's own default (the YAML/cron default of 6).
    assert all(c.max_hops == 6 for c in config.capabilities)


def test_build_assigns_unique_credential_env_names() -> None:
    config = build_customer_config(_payload(), customer_id="org_123")
    names = {c.env_var_user for c in config.role_credentials} | {
        c.env_var_password for c in config.role_credentials
    }
    # one user + one password per role, all distinct
    assert len(names) == 2 * len(config.role_credentials)
    assert [c.role for c in config.role_credentials] == ["admin", "viewer"]


@pytest.mark.parametrize(
    "mutate, needle",
    [
        ({"sandbox_url": ""}, "sandbox_url"),
        ({"roles": []}, "roles"),
        ({"capabilities": []}, "capabilities"),
    ],
)
def test_build_rejects_incomplete_payload(mutate: dict, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        build_customer_config(_payload(**mutate), customer_id="org_123")


# ── apply_pulled_credentials ───────────────────────────────────────────────


def test_apply_credentials_sets_env_and_returns_roles() -> None:
    config = build_customer_config(_payload(), customer_id="org_123")
    env: dict[str, str] = {CREDENTIALS_ENV: _creds("admin", "viewer")}

    applied = apply_pulled_credentials(config, environ=env)

    assert applied == ["admin", "viewer"]
    admin = next(c for c in config.role_credentials if c.role == "admin")
    assert env[admin.env_var_user] == "admin@x.io"
    assert env[admin.env_var_password] == "pw-admin"


def test_apply_credentials_requires_every_role() -> None:
    config = build_customer_config(_payload(), customer_id="org_123")
    env = {CREDENTIALS_ENV: _creds("admin")}  # viewer missing
    with pytest.raises(ValueError, match="viewer"):
        apply_pulled_credentials(config, environ=env)


def test_apply_credentials_empty_raises() -> None:
    config = build_customer_config(_payload(), customer_id="org_123")
    with pytest.raises(ValueError, match="empty"):
        apply_pulled_credentials(config, environ={})


def test_apply_credentials_bad_json_raises() -> None:
    config = build_customer_config(_payload(), customer_id="org_123")
    with pytest.raises(ValueError, match="valid credential JSON"):
        apply_pulled_credentials(config, environ={CREDENTIALS_ENV: "{not json"})


# ── run-dispatch CLI ───────────────────────────────────────────────────────


def test_run_dispatch_invokes_run_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run_crawl(*, customer_id, snapshot_version, config):
        captured["customer_id"] = customer_id
        captured["snapshot_version"] = snapshot_version
        captured["config"] = config
        return "r2://pack"

    monkeypatch.setattr(cli_module, "run_crawl", fake_run_crawl)
    monkeypatch.setenv("CONFIG_JSON", json.dumps(_payload()))
    monkeypatch.setenv(CREDENTIALS_ENV, _creds("admin", "viewer"))

    result = CliRunner().invoke(
        cli,
        ["run-dispatch", "--customer", "org_123", "--crawl-date", "2026-06-14"],
    )

    assert result.exit_code == 0, result.output
    assert captured["customer_id"] == "org_123"
    # snapshot defaults to the ISO week of the crawl date
    assert captured["snapshot_version"] == "2026-W24"
    assert captured["config"].sandbox_base_url == "https://sandbox.example.com"
    assert "Pack written to: r2://pack" in result.output


def test_run_dispatch_explicit_snapshot_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        cli_module,
        "run_crawl",
        lambda **kw: captured.update(kw) or "r2://pack",
    )
    monkeypatch.setenv("CONFIG_JSON", json.dumps(_payload()))
    monkeypatch.setenv(CREDENTIALS_ENV, _creds("admin", "viewer"))

    result = CliRunner().invoke(
        cli,
        [
            "run-dispatch",
            "--customer",
            "org_123",
            "--crawl-date",
            "2026-06-14",
            "--snapshot",
            "2026-W99",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["snapshot_version"] == "2026-W99"


def test_run_dispatch_missing_config_json_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONFIG_JSON", raising=False)
    result = CliRunner().invoke(
        cli,
        ["run-dispatch", "--customer", "org_123", "--crawl-date", "2026-06-14"],
    )
    assert result.exit_code != 0
    assert "empty" in result.output


def test_run_dispatch_missing_credentials_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # config_json present but no credentials pulled — a real tenant must fail
    # loudly rather than walk unauthenticated.
    monkeypatch.setenv("CONFIG_JSON", json.dumps(_payload()))
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    result = CliRunner().invoke(
        cli,
        ["run-dispatch", "--customer", "org_123", "--crawl-date", "2026-06-14"],
    )
    assert result.exit_code != 0
    assert CREDENTIALS_ENV in result.output


def test_run_dispatch_honesty_failure_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kw):
        raise HonestyGateError(HonestyVerdict(coverage=[], crashes={"onboarding/admin": "boom"}))

    monkeypatch.setattr(cli_module, "run_crawl", boom)
    monkeypatch.setenv("CONFIG_JSON", json.dumps(_payload()))
    monkeypatch.setenv(CREDENTIALS_ENV, _creds("admin", "viewer"))

    result = CliRunner().invoke(
        cli,
        ["run-dispatch", "--customer", "org_123", "--crawl-date", "2026-06-14"],
    )

    assert result.exit_code != 0
    assert "honesty gate failed — nothing was published" in result.output
