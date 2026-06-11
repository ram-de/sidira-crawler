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
    capabilities: list[CapabilityConfig]
    role_credentials: list[RoleCredential] = Field(default_factory=list)


def load_customer_config(path: Path | str) -> CustomerConfig:
    """Load a customer's crawl configuration from YAML."""
    raw = yaml.safe_load(Path(path).read_text())
    return CustomerConfig.model_validate(raw)
