"""LLM client with provider preference: Groq → Anthropic → fallback.

Mirrors the pattern used in pythia-personas so credentials and behavior stay
consistent across producers.
"""

from __future__ import annotations

import os
from typing import Protocol


class LLMClient(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str: ...


class GroqClient:
    """Groq via OpenAI-compatible endpoint."""

    def __init__(self, model: str = "llama-3.3-70b-versatile") -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=os.environ["GROQ_API_KEY"],
            base_url="https://api.groq.com/openai/v1",
        )
        self._model = model

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


class AnthropicClient:
    """Anthropic Claude fallback."""

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = model

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        resp = self._client.messages.create(
            model=self._model,
            system=system,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text


def get_client() -> LLMClient:
    """Return the first available LLM client based on env-var presence."""
    if os.environ.get("GROQ_API_KEY"):
        return GroqClient()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    raise RuntimeError(
        "No LLM API key found. Set GROQ_API_KEY (preferred) or ANTHROPIC_API_KEY."
    )
