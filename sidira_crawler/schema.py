"""Feature Pack schema.

These models are duplicated here until sidira-schemas exists. When that
shared package lands, import from it instead and delete this file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ComponentState(BaseModel):
    """One observed state of a UI component (default / pressed / disabled / error / empty)."""

    name: str
    text: str | None = Field(default=None, description="Product copy shown in this state")
    annotations: dict[str, str] = Field(default_factory=dict)


class Component(BaseModel):
    """A UI component within a screen (button, card, input, modal, ...)."""

    id: str
    role: str
    states: list[ComponentState] = Field(default_factory=list)
    text: str | None = Field(default=None, description="Product copy displayed by this component")
    visible_to_roles: list[str] | None = None


class Transition(BaseModel):
    """An observed transition from one screen to another.

    ``external`` marks a boundary edge: the source screen is inside this
    capability's cluster but the target landed in a *different* capability (or
    stayed unassigned). Clustering splits a product into capabilities, but the
    user's observed entry/exit paths cross those seams — keeping boundary edges
    (source-in-cluster) instead of dropping them preserves those real
    cross-capability flows. ``to_screen`` still names the external target so a
    consumer can resolve which capability it belongs to. (sidira-crawler#14)
    """

    trigger: str
    from_screen: str
    to_screen: str
    role_gated: list[str] | None = None
    external: bool = False


class Screen(BaseModel):
    """A frame in the reverse-Figma spec — one logical view in the product."""

    id: str
    name: str
    role: str | None = None
    components: list[Component] = Field(default_factory=list)


class ReverseFigma(BaseModel):
    """Structured visual + interactive spec — same shape Figma communicates."""

    screens: list[Screen] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)


class Capability(BaseModel):
    """One capability — the unit of analysis. Has a reverse-PRD + reverse-Figma."""

    name: str
    surfaces: list[str]
    roles: list[str]
    entry_points: list[str]
    reverse_prd: str
    reverse_figma: ReverseFigma
    implicit_metric: str | None = None
    #: Higher-level grouping label (capability → domain), synthesized by the
    #: second-level clustering pass. Optional and back-compatible: packs
    #: predating crawler #18 (and any pack whose domain pass degraded) omit it,
    #: and the consumer buckets a domainless capability as "Ungrouped".
    domain: str | None = None


class FeaturePackManifest(BaseModel):
    """Top-level index for a weekly snapshot."""

    customer_id: str
    #: Which crawl setup of the org this pack belongs to (sidira-simulator
    #: #215/#245). ``"default"`` is the legacy single-setup org whose pack lives
    #: at ``customers/<id>/featurepacks`` (byte-identical to pre-multi-setup
    #: behavior); any other value records the setup so ``storage`` can derive the
    #: per-setup ``customers/<id>/setups/<product_id>/featurepacks`` prefix.
    #: Optional + back-compat: packs predating multi-setup omit it and default to
    #: ``"default"``.
    product_id: str = "default"
    snapshot_version: str
    generated_at: datetime
    schema_version: Literal["1.0"] = "1.0"
    capabilities: list[str]
    #: Persisted index for the second-level grouping (capability name → domain).
    #: This is the artifact-store home for `Capability.domain` — per-capability
    #: files carry only the reverse-PRD/Figma, so the manifest is the only place
    #: a consumer can read grouping in a single read. Optional and back-compat:
    #: empty for packs predating crawler #18 or whose domain pass degraded; a
    #: capability absent from the map falls back to "Ungrouped".
    capability_domains: dict[str, str] = Field(default_factory=dict)
    crawl_metadata: dict[str, str] = Field(default_factory=dict)


class FeaturePack(BaseModel):
    """Full Feature Pack — manifest plus all capabilities."""

    manifest: FeaturePackManifest
    capabilities: list[Capability]
