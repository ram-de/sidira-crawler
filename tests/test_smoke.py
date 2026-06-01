"""Smoke tests — verify imports, schema round-trips, and CLI wiring."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from sidira_crawler import __version__
from sidira_crawler.cli import cli
from sidira_crawler.config import load_customer_config
from sidira_crawler.schema import (
    Capability,
    FeaturePack,
    FeaturePackManifest,
    ReverseFigma,
)
from sidira_crawler.storage import LocalArtifactStore


def test_version_constant() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Feature Pack producer" in result.output


def test_schema_round_trip() -> None:
    pack = FeaturePack(
        manifest=FeaturePackManifest(
            customer_id="acme",
            snapshot_version="2026-W21",
            generated_at=datetime.now(timezone.utc),
            capabilities=["reorder"],
        ),
        capabilities=[
            Capability(
                name="reorder",
                surfaces=["/home"],
                roles=["member"],
                entry_points=["/home"],
                reverse_prd="# Reorder\n\n## User goal\nReorder the last meal.\n",
                reverse_figma=ReverseFigma(),
            )
        ],
    )
    blob = pack.model_dump_json()
    restored = FeaturePack.model_validate_json(blob)
    assert restored.manifest.customer_id == "acme"
    assert restored.capabilities[0].name == "reorder"


def test_example_config_loads() -> None:
    cfg = load_customer_config(Path("config/customers/example_customer.yaml"))
    assert cfg.customer_id == "example"
    assert {c.name for c in cfg.capabilities} == {"reorder", "checkout"}
    assert {c.role for c in cfg.role_credentials} == {"guest", "member", "dashpass_member"}


def test_local_store_writes_pack(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    pack = FeaturePack(
        manifest=FeaturePackManifest(
            customer_id="acme",
            snapshot_version="2026-W21",
            generated_at=datetime.now(timezone.utc),
            capabilities=["reorder"],
        ),
        capabilities=[
            Capability(
                name="reorder",
                surfaces=["/home"],
                roles=["member"],
                entry_points=["/home"],
                reverse_prd="# Reorder\n",
                reverse_figma=ReverseFigma(),
            )
        ],
    )
    uri = store.write_pack(pack)
    base = Path(uri)
    assert (base / "manifest.json").exists()
    assert (base / "capabilities" / "reorder" / "reverse-prd.md").read_text().startswith("# Reorder")
    assert (base / "capabilities" / "reorder" / "reverse-figma.json").exists()


@pytest.mark.skip(reason="full crawl requires Playwright + LLM; implement in Phase 1")
def test_run_crawl_end_to_end() -> None:
    pass
