"""Tests for llm_factory.build_llm_client — env-var backend selection.

Story 61-9 / ADR-101 amendment retired the legacy ``claude`` and
``ollama`` backends for both narrator and tool purposes. The factory
raises :class:`NarratorBackendRetired` at construction for those env
values; ``anthropic_sdk`` is the sole viable backend. Whitespace and
case-insensitivity normalization still applies (the gate uses the
normalized key), so a stray casing of a retired name fails loud rather
than silently passing through.
"""

from __future__ import annotations

import pytest

from sidequest.agents.llm_factory import (
    NarratorBackendRetired,
    UnknownBackend,
    build_llm_client,
)


def test_default_is_anthropic_sdk(monkeypatch):
    """Phase D: default backend flipped from claude to anthropic_sdk."""
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    monkeypatch.delenv("SIDEQUEST_LLM_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = build_llm_client()
    assert isinstance(client, AnthropicSdkClient)


def test_explicit_claude_backend_is_retired(monkeypatch):
    """Story 61-9 / ADR-101 amendment: ``claude`` is retired for any
    purpose. The factory fails loud at construction."""
    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", "claude")
    with pytest.raises(NarratorBackendRetired):
        build_llm_client()


def test_ollama_backend_is_retired(monkeypatch):
    """Story 61-9 / ADR-101 amendment: ``ollama`` is retired for any
    purpose. The factory fails loud at construction even with a custom
    ``SIDEQUEST_OLLAMA_URL`` set."""
    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", "ollama")
    monkeypatch.setenv("SIDEQUEST_OLLAMA_URL", "http://example.local:9000")
    with pytest.raises(NarratorBackendRetired):
        build_llm_client()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", "gpt4")
    with pytest.raises(UnknownBackend):
        build_llm_client()


def test_whitespace_and_case_insensitivity_still_normalizes_into_gate(monkeypatch):
    """Stray casing of a retired backend must still hit the gate, not
    silently pass through as an unrecognized backend. The normalization
    (``strip().lower()``) runs before the gate check, so ``' CLAUDE  '``
    is treated identically to ``'claude'``."""
    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", " CLAUDE  ")
    with pytest.raises(NarratorBackendRetired):
        build_llm_client()
    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", "Ollama")
    with pytest.raises(NarratorBackendRetired):
        build_llm_client()


def test_anthropic_sdk_backend_key_routes_to_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
    from sidequest.agents.llm_factory import build_llm_client

    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", "anthropic_sdk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = build_llm_client()
    assert isinstance(client, AnthropicSdkClient)
