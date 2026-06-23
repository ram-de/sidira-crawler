"""Customer config loader.

A customer config declares the capabilities the crawler should walk this week,
their entry points, and the roles that experience each one. This is the MVP
input that lets us skip Phase-2 LLM capability synthesis.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RoleCredential(BaseModel):
    """Sandbox credentials for one role. Loaded from env at runtime, never YAML."""

    role: str
    env_var_user: str
    env_var_password: str


class CapabilityConfig(BaseModel):
    """A single capability declared by the customer for this week's crawl."""

    name: str
    entry_points: list[str]
    roles: list[str]
    # Depth budget in user-action hops from each entry point. The longest flow
    # the walker must capture today is 4 hops; 6 sits comfortably above that,
    # so a missed screen is a walker failure, never budget exhaustion.
    max_hops: int = 6


class CustomerConfig(BaseModel):
    """Per-customer crawl configuration."""

    customer_id: str
    sandbox_base_url: str
    # Which crawl setup of this org this pack belongs to (sidira-simulator
    # #215/#245). ``"default"`` is the legacy single-setup org, whose pack lives
    # at the original ``customers/<id>/featurepacks`` prefix (byte-identical to
    # pre-multi-setup behavior); any other value routes the pack under
    # ``customers/<id>/setups/<product_id>/featurepacks`` so two setups of one
    # org never collide. Validated as a safe R2-key slug in
    # :func:`sidira_crawler.dispatch.build_customer_config`.
    product_id: str = "default"
    capabilities: list[CapabilityConfig]
    role_credentials: list[RoleCredential] = Field(default_factory=list)
    # Opt-in cap×role walk parallelism (sidira-crawler#28 lever 2). Each
    # capability×role walk is fully isolated (own browser + fresh login), so
    # they are safe to run on separate threads. Default ``1`` = serial, the only
    # universally-safe setting: many apps enforce single-session-per-user, so N
    # concurrent logins of the same role account would invalidate each other's
    # cookies mid-walk and crash the gate. Raise only for customers whose test
    # accounts tolerate concurrent sessions. Bounds the thread pool over the
    # cap×role work units; the determinism re-walk always runs serially.
    walk_concurrency: int = 1


def load_customer_config(path: Path | str) -> CustomerConfig:
    """Load a customer's crawl configuration from YAML."""
    raw = yaml.safe_load(Path(path).read_text())
    return CustomerConfig.model_validate(raw)
