"""Artifact storage adapter — writes Feature Pack snapshots to R2 (S3-compatible).

Layout:
    customers/<customer_id>/featurepacks/<snapshot_version>/
        manifest.json
        capabilities/<name>/
            reverse-prd.md
            reverse-figma.json
        diff-vs-prior.json
    customers/<customer_id>/featurepacks/latest   ← pointer, contents = snapshot_version

A non-default crawl setup (sidira-simulator #215/#245) writes the same tree
under a per-setup prefix instead — ``customers/<customer_id>/setups/<product_id>/
featurepacks/...`` — so two setups of one org never collide. See
:func:`featurepacks_prefix`, the single source of truth shared with the reader.

The ``latest`` pointer is what downstream consumers (the simulator's
FeaturePackLoader) resolve before reading a pack. Writing it is the publish
step: call ``publish_latest`` only after every snapshot artifact (pack AND
diff) is written, so the pointer never names a partially written snapshot.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol

import structlog

from sidira_crawler.schema import FeaturePack

log = structlog.get_logger()

# A crawl setup id (``product_id``) becomes a path segment in the R2 key
# ``customers/<id>/setups/<product_id>/featurepacks/...``. Constrain it to a safe
# slug — lowercase alphanumerics plus ``-``/``_``, bounded length — so it can
# never inject a path separator (``/``), traverse (``..``), or smuggle
# whitespace into an object key. ``"default"`` is the legacy single-setup value
# and is allowed as a special case (it produces the byte-identical legacy path).
_PRODUCT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_product_id(product_id: str) -> str:
    """Return ``product_id`` if it is a safe R2-key slug, else raise ValueError.

    ``"default"`` is always accepted (legacy single-setup). Every other value
    must match :data:`_PRODUCT_ID_RE`. This is the single enforcement point for
    the slug invariant: :func:`featurepacks_prefix` calls it, so NO key can be
    built from an unsafe id regardless of which entry point (dispatch, manual
    CLI, mock workflow) supplied it. The simulator's dispatch adapter reuses
    this so the producer and the dispatch validator can never drift.
    """
    if product_id == "default" or _PRODUCT_ID_RE.match(product_id):
        return product_id
    raise ValueError(
        f"invalid product_id {product_id!r} — must be a lowercase slug "
        "([a-z0-9][a-z0-9_-]{0,63}); no '/', '..', or spaces"
    )


def featurepacks_prefix(customer_id: str, product_id: str = "default") -> str:
    """The R2/local key prefix (no trailing slash) for a customer's Feature Packs.

    This is the single source of truth for the pack path, shared by the writer
    (this module) and — by contract — the simulator's ``FeaturePackLoader``
    reader (sidira-simulator #215/#245):

    * ``product_id == "default"`` → ``customers/<id>/featurepacks`` — the legacy
      single-setup path, **byte-identical** to pre-multi-setup behavior, so the
      live Founders org's pack is untouched.
    * any other ``product_id`` → ``customers/<id>/setups/<product_id>/featurepacks``
      so two crawl setups of one org never collide.

    ``product_id`` is validated here (:func:`validate_product_id`) so this choke
    point can never emit a key containing ``/``, ``..``, or whitespace — even if
    a caller bypassed the dispatch validator. Every path-building site in this
    module routes through here, which is what keeps a named setup's ``latest``
    pointer (and its publish/skip-regression logic) independent of the default
    setup's pointer.
    """
    validate_product_id(product_id)
    if product_id == "default":
        return f"customers/{customer_id}/featurepacks"
    return f"customers/{customer_id}/setups/{product_id}/featurepacks"


class ArtifactStore(Protocol):
    def write_pack(self, pack: FeaturePack) -> str: ...


class LocalArtifactStore:
    """Writes to a local directory. Useful for dev and integration tests."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def write_pack(self, pack: FeaturePack) -> str:
        prefix = featurepacks_prefix(pack.manifest.customer_id, pack.manifest.product_id)
        base = self.root / prefix / pack.manifest.snapshot_version
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
        prefix = featurepacks_prefix(pack.manifest.customer_id, pack.manifest.product_id)
        base_key = f"{prefix}/{pack.manifest.snapshot_version}"
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


def publish_latest(
    store: ArtifactStore,
    customer_id: str,
    snapshot_version: str,
    product_id: str = "default",
) -> bool:
    """Point ``<prefix>/latest`` at a fully written snapshot.

    The prefix is per-setup (see :func:`featurepacks_prefix`): a named setup's
    pointer lives under ``customers/<id>/setups/<product_id>/featurepacks/latest``
    and is never read or overwritten by the default setup (or any sibling), so
    the publish/skip-regression logic below is fully per-setup.

    This is the publish step — consumers resolve the pointer before reading,
    so call it only after every snapshot artifact (pack and diff) is written.

    A backfill or re-synthesis of an old snapshot must not regress the
    pointer: if the current pointer names a newer snapshot, the publish is
    skipped. Weekly buckets are zero-padded (``2026-W04``), so plain string
    order is chronological. Returns True when the pointer was updated.
    """
    prefix = featurepacks_prefix(customer_id, product_id)
    current = _read_latest(store, customer_id, product_id)
    if current is not None and current > snapshot_version:
        log.info(
            "publish.latest.skipped",
            customer_id=customer_id,
            product_id=product_id,
            current=current,
            snapshot=snapshot_version,
        )
        return False
    if isinstance(store, LocalArtifactStore):
        path = store.root / prefix / "latest"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot_version)
    elif isinstance(store, R2ArtifactStore):
        store._put(f"{prefix}/latest", snapshot_version)
    return True


def _read_latest(store: ArtifactStore, customer_id: str, product_id: str = "default") -> str | None:
    """Read the current latest pointer, or None if it has never been published."""
    prefix = featurepacks_prefix(customer_id, product_id)
    if isinstance(store, LocalArtifactStore):
        path = store.root / prefix / "latest"
        return path.read_text().strip() if path.exists() else None
    if isinstance(store, R2ArtifactStore):
        key = f"{prefix}/latest"
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
    product_id: str = "default",
) -> None:
    """Write the diff-vs-prior snapshot summary alongside the pack."""
    prefix = featurepacks_prefix(customer_id, product_id)
    if isinstance(store, LocalArtifactStore):
        out = store.root / prefix / snapshot_version / "diff-vs-prior.json"
        out.write_text(json.dumps(diff, indent=2))
    elif isinstance(store, R2ArtifactStore):
        key = f"{prefix}/{snapshot_version}/diff-vs-prior.json"
        store._put(key, json.dumps(diff, indent=2))
