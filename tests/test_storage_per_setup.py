"""Per-setup Feature Pack path tests (sidira-simulator #215/#245).

These lock the path contract shared with the simulator's ``FeaturePackLoader``:

* ``product_id == "default"`` → ``customers/<id>/featurepacks`` — the legacy
  single-setup path, **byte-identical** to pre-multi-setup behavior (the live
  Founders org's pack lives there, so this is non-negotiable).
* any other ``product_id`` → ``customers/<id>/setups/<product_id>/featurepacks``.

The default-key assertions use literal pre-change strings on purpose: if a
refactor ever drifts the default path, these fail loudly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sidira_crawler.schema import (
    Capability,
    FeaturePack,
    FeaturePackManifest,
    ReverseFigma,
)
from sidira_crawler.storage import (
    LocalArtifactStore,
    _read_latest,
    featurepacks_prefix,
    publish_latest,
    write_diff,
)


def _pack(product_id: str = "default", customer_id: str = "acme") -> FeaturePack:
    manifest_kwargs = dict(
        customer_id=customer_id,
        snapshot_version="2026-W21",
        generated_at=datetime.now(timezone.utc),
        capabilities=["reorder"],
    )
    # Only pass product_id when non-default, to prove the manifest default is
    # itself "default" (a pack predating multi-setup omits the field entirely).
    if product_id != "default":
        manifest_kwargs["product_id"] = product_id
    return FeaturePack(
        manifest=FeaturePackManifest(**manifest_kwargs),
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


# ── featurepacks_prefix: the exact contract strings ────────────────────────


def test_prefix_default_is_legacy_byte_identical() -> None:
    # Literal pre-change string — NO setups/ segment.
    assert featurepacks_prefix("acme") == "customers/acme/featurepacks"
    assert featurepacks_prefix("acme", "default") == "customers/acme/featurepacks"


def test_prefix_named_setup_has_setups_segment() -> None:
    assert (
        featurepacks_prefix("acme", "growth-v2") == "customers/acme/setups/growth-v2/featurepacks"
    )


# ── default path is byte-identical to current main ─────────────────────────


def test_default_write_keys_byte_identical(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    uri = store.write_pack(_pack())  # default

    # Literal expected layout — exactly what main produced.
    base = tmp_path / "customers" / "acme" / "featurepacks" / "2026-W21"
    assert Path(uri) == base
    assert (base / "manifest.json").exists()
    assert (base / "capabilities" / "reorder" / "reverse-prd.md").exists()
    assert (base / "capabilities" / "reorder" / "reverse-figma.json").exists()
    # The relative key set, asserted as literal strings.
    rel = sorted(str(p.relative_to(tmp_path)) for p in base.rglob("*") if p.is_file())
    assert rel == [
        "customers/acme/featurepacks/2026-W21/capabilities/reorder/reverse-figma.json",
        "customers/acme/featurepacks/2026-W21/capabilities/reorder/reverse-prd.md",
        "customers/acme/featurepacks/2026-W21/manifest.json",
    ]


def test_default_latest_pointer_byte_identical(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.write_pack(_pack())
    assert publish_latest(store, "acme", "2026-W21") is True
    pointer = tmp_path / "customers" / "acme" / "featurepacks" / "latest"
    assert pointer.read_text() == "2026-W21"


def test_default_diff_key_byte_identical(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.write_pack(_pack())
    write_diff(store, "acme", "2026-W21", {"note": "x"})
    diff = tmp_path / "customers" / "acme" / "featurepacks" / "2026-W21" / "diff-vs-prior.json"
    assert diff.exists()


# ── named-setup path lands under setups/<id>/ ──────────────────────────────


def test_named_setup_write_keys(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    uri = store.write_pack(_pack(product_id="growth-v2"))

    base = tmp_path / "customers" / "acme" / "setups" / "growth-v2" / "featurepacks" / "2026-W21"
    assert Path(uri) == base
    assert (base / "manifest.json").exists()
    assert (base / "capabilities" / "reorder" / "reverse-prd.md").exists()
    # The legacy default path must NOT exist for a named setup.
    assert not (tmp_path / "customers" / "acme" / "featurepacks").exists()


def test_named_setup_latest_and_diff(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.write_pack(_pack(product_id="growth-v2"))
    publish_latest(store, "acme", "2026-W21", product_id="growth-v2")
    write_diff(store, "acme", "2026-W21", {"note": "x"}, product_id="growth-v2")
    setup_base = tmp_path / "customers" / "acme" / "setups" / "growth-v2" / "featurepacks"
    assert (setup_base / "latest").read_text() == "2026-W21"
    assert (setup_base / "2026-W21" / "diff-vs-prior.json").exists()


# ── pointer independence: a named setup never touches default's pointer ─────


def test_named_setup_pointer_independent_of_default(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    # Publish the default setup's pointer.
    store.write_pack(_pack())
    publish_latest(store, "acme", "2026-W21")
    assert _read_latest(store, "acme") == "2026-W21"

    # The named setup has never been published — its pointer reads None,
    # i.e. it does NOT see the default setup's pointer.
    assert _read_latest(store, "acme", "growth-v2") is None

    # Publish the named setup at a DIFFERENT (newer) snapshot; the default
    # pointer must be untouched, and the named pointer must be its own value.
    store.write_pack(_pack(product_id="growth-v2"))
    # Use a deliberately newer-then-older sequence to exercise skip logic too.
    publish_latest(store, "acme", "2026-W30", product_id="growth-v2")
    assert _read_latest(store, "acme", "growth-v2") == "2026-W30"
    assert _read_latest(store, "acme") == "2026-W21"  # default UNCHANGED


def test_named_setup_skip_regression_is_per_setup(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    # Named setup at a NEWER snapshot.
    publish_latest(store, "acme", "2026-W30", product_id="growth-v2")
    # A backfill of an OLDER snapshot for the same setup is skipped...
    assert publish_latest(store, "acme", "2026-W10", product_id="growth-v2") is False
    assert _read_latest(store, "acme", "growth-v2") == "2026-W30"
    # ...but the DEFAULT setup's first publish at the same older snapshot
    # succeeds — the skip logic reads only its own pointer.
    assert publish_latest(store, "acme", "2026-W10") is True
    assert _read_latest(store, "acme") == "2026-W10"


# ── round-trip: manifest path == what the simulator loader would read ───────


def test_round_trip_named_setup_manifest_path_matches_loader(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    pack = _pack(product_id="growth-v2")
    uri = store.write_pack(pack)

    # The simulator's FeaturePackLoader computes the prefix from
    # (customer_id, product_id); reconstruct it and confirm the written
    # manifest sits exactly where the reader will look.
    expected_prefix = featurepacks_prefix(pack.manifest.customer_id, pack.manifest.product_id)
    loader_manifest = tmp_path / expected_prefix / pack.manifest.snapshot_version / "manifest.json"
    assert loader_manifest.exists()
    assert Path(uri) == loader_manifest.parent
    # And the manifest records the setup it belongs to (provenance).
    assert pack.manifest.product_id == "growth-v2"


def test_manifest_default_product_id_when_absent() -> None:
    # A pack constructed without product_id defaults to "default" → legacy path.
    pack = _pack()
    assert pack.manifest.product_id == "default"


# ── CLI: `synthesize --product-id` routes the whole write through the setup ─


def test_synthesize_cli_threads_product_id(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from sidira_crawler.cli import cli

    # Stub the LLM + synthesis so the test stays hermetic; the point is that
    # --product-id rides onto the manifest and the LocalArtifactStore writes the
    # whole tree (pack + diff + latest) under the named-setup prefix. cli.py
    # imports these names at call time, so patch them on their home modules.
    monkeypatch.setattr("sidira_crawler.llm.get_client", lambda: object())

    def fake_synthesize_pack(graph, llm, customer_id, snapshot_version, product_id="default"):
        return _pack(product_id=product_id, customer_id=customer_id)

    monkeypatch.setattr("sidira_crawler.synthesis.pack.synthesize_pack", fake_synthesize_pack)
    monkeypatch.setattr(
        "sidira_crawler.synthesis.graph.load_raw_jsonl",
        lambda path: type("G", (), {"run_date": "2026-05-25"})(),
    )

    raw = tmp_path / "raw.jsonl"
    raw.write_text("{}\n")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "synthesize",
            "--raw",
            str(raw),
            "--customer",
            "acme",
            "--snapshot",
            "2026-W21",
            "--product-id",
            "growth-v2",
            "--out",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    setup_base = out_dir / "customers" / "acme" / "setups" / "growth-v2" / "featurepacks"
    assert (setup_base / "2026-W21" / "manifest.json").exists()
    assert (setup_base / "2026-W21" / "diff-vs-prior.json").exists()
    assert (setup_base / "latest").read_text() == "2026-W21"
    # The legacy default path must be untouched.
    assert not (out_dir / "customers" / "acme" / "featurepacks").exists()
