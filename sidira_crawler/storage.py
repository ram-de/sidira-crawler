"""Artifact storage adapter — writes Feature Pack snapshots to R2 (S3-compatible).

Layout:
    customers/<customer_id>/featurepacks/<snapshot_version>/
        manifest.json
        capabilities/<name>/
            reverse-prd.md
            reverse-figma.json
        diff-vs-prior.json
    customers/<customer_id>/featurepacks/latest   ← pointer, contents = snapshot_version

The ``latest`` pointer is what downstream consumers (the simulator's
FeaturePackLoader) resolve before reading a pack. Writing it is the publish
step: call ``publish_latest`` only after every snapshot artifact (pack AND
diff) is written, so the pointer never names a partially written snapshot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

import structlog

from sidira_crawler.schema import FeaturePack

log = structlog.get_logger()


class ArtifactStore(Protocol):
    def write_pack(self, pack: FeaturePack) -> str: ...


class LocalArtifactStore:
    """Writes to a local directory. Useful for dev and integration tests."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def write_pack(self, pack: FeaturePack) -> str:
        base = (
            self.root
            / "customers"
            / pack.manifest.customer_id
            / "featurepacks"
            / pack.manifest.snapshot_version
        )
        base.mkdir(parents=True, exist_ok=True)
        (base / "manifest.json").write_text(pack.manifest.model_dump_json(indent=2))
        caps_dir = base / "capabilities"
        caps_dir.mkdir(exist_ok=True)
        for cap in pack.capabilities:
            cap_dir = caps_dir / cap.name
            cap_dir.mkdir(exist_ok=True)
            (cap_dir / "reverse-prd.md").write_text(cap.reverse_prd)
            (cap_dir / "reverse-figma.json").write_text(cap.reverse_figma.model_dump_json(indent=2))
        return str(base)


class R2ArtifactStore:
    """Writes to Cloudflare R2 via the S3-compatible API."""

    def __init__(self, bucket: str | None = None) -> None:
        import boto3

        self._bucket = bucket or os.environ["R2_BUCKET"]
        self._s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        )

    def write_pack(self, pack: FeaturePack) -> str:
        base_key = (
            f"customers/{pack.manifest.customer_id}"
            f"/featurepacks/{pack.manifest.snapshot_version}"
        )
        self._put(f"{base_key}/manifest.json", pack.manifest.model_dump_json(indent=2))
        for cap in pack.capabilities:
            self._put(f"{base_key}/capabilities/{cap.name}/reverse-prd.md", cap.reverse_prd)
            self._put(
                f"{base_key}/capabilities/{cap.name}/reverse-figma.json",
                cap.reverse_figma.model_dump_json(indent=2),
            )
        return f"r2://{self._bucket}/{base_key}"

    def _put(self, key: str, body: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=body.encode("utf-8"))


def get_store() -> ArtifactStore:
    """Select store from env: R2 when R2_BUCKET is set, otherwise local."""
    if os.environ.get("R2_BUCKET"):
        return R2ArtifactStore()
    local_root = os.environ.get("PYTHIA_CRAWLER_LOCAL_OUTPUT", "outputs")
    return LocalArtifactStore(local_root)


def publish_latest(store: ArtifactStore, customer_id: str, snapshot_version: str) -> bool:
    """Point ``customers/<id>/featurepacks/latest`` at a fully written snapshot.

    This is the publish step — consumers resolve the pointer before reading,
    so call it only after every snapshot artifact (pack and diff) is written.

    A backfill or re-synthesis of an old snapshot must not regress the
    pointer: if the current pointer names a newer snapshot, the publish is
    skipped. Weekly buckets are zero-padded (``2026-W04``), so plain string
    order is chronological. Returns True when the pointer was updated.
    """
    current = _read_latest(store, customer_id)
    if current is not None and current > snapshot_version:
        log.info(
            "publish.latest.skipped",
            customer_id=customer_id,
            current=current,
            snapshot=snapshot_version,
        )
        return False
    if isinstance(store, LocalArtifactStore):
        path = store.root / "customers" / customer_id / "featurepacks" / "latest"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot_version)
    elif isinstance(store, R2ArtifactStore):
        store._put(f"customers/{customer_id}/featurepacks/latest", snapshot_version)
    return True


def _read_latest(store: ArtifactStore, customer_id: str) -> str | None:
    """Read the current latest pointer, or None if it has never been published."""
    if isinstance(store, LocalArtifactStore):
        path = store.root / "customers" / customer_id / "featurepacks" / "latest"
        return path.read_text().strip() if path.exists() else None
    if isinstance(store, R2ArtifactStore):
        key = f"customers/{customer_id}/featurepacks/latest"
        try:
            obj = store._s3.get_object(Bucket=store._bucket, Key=key)
        except store._s3.exceptions.NoSuchKey:
            return None
        return obj["Body"].read().decode("utf-8").strip()
    return None


def write_diff(
    store: ArtifactStore,
    customer_id: str,
    snapshot_version: str,
    diff: dict,
) -> None:
    """Write the diff-vs-prior snapshot summary alongside the pack."""
    if isinstance(store, LocalArtifactStore):
        out = (
            store.root
            / "customers"
            / customer_id
            / "featurepacks"
            / snapshot_version
            / "diff-vs-prior.json"
        )
        out.write_text(json.dumps(diff, indent=2))
    elif isinstance(store, R2ArtifactStore):
        key = f"customers/{customer_id}/featurepacks/{snapshot_version}/diff-vs-prior.json"
        store._put(key, json.dumps(diff, indent=2))
