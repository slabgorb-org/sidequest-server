"""Story 119-3 RED — dependency declaration + the ``query`` transport seam.

The port replaces the raw ``anthropic`` Messages SDK transport with
``claude-agent-sdk`` over subscription auth. Two first-step wiring facts this
file pins (spec §10 "Dependency" + §6.2/OQ-9 "the fake-``query`` seam"):

1. ``claude-agent-sdk`` is a declared, importable dependency of the server.
   Confirmed 2026-06-16 it is NOT currently in ``pyproject.toml`` — the spikes
   pulled it via ``uv run --with claude-agent-sdk``. Declaring it is the first
   Dev step; a ``--with`` shim must not be the production import path.

2. Each module that drives the Agent SDK exposes a **module-level ``query``
   symbol** (the analog of ``build_async_anthropic``, late-bound through the
   module dict) so the whole RED/CI fleet can inject a fake transport and drive
   convergence without a live subscription. The behavioural suites
   (``test_119_3_narrator_port`` / ``_haiku_port``) prove the symbol is
   actually *called*; this file pins that the seam exists and is the SDK's
   ``query`` entry point.

RED: ``claude_agent_sdk`` is uninstalled and neither module imports ``query``,
so every test here fails. GREEN: Dev declares the dep + wires the seam.
"""

from __future__ import annotations

from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_claude_agent_sdk_is_importable() -> None:
    """The production transport must be a real, installed import — not a
    ``uv run --with`` shim (No Stubbing / wiring discipline)."""
    import claude_agent_sdk  # noqa: F401 — import IS the assertion

    # The two symbols the port's canonical surface uses (spec §3.1).
    from claude_agent_sdk import ClaudeAgentOptions, query  # noqa: F401


def test_claude_agent_sdk_declared_in_pyproject() -> None:
    """A declared dependency (dependency-hygiene, python.md #12): the import
    test above can pass off a transitively-present package, so also pin that
    the server *declares* it — otherwise a future ``uv sync`` drops it."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    assert "claude-agent-sdk" in text or "claude_agent_sdk" in text, (
        "claude-agent-sdk must be a declared dependency of sidequest-server "
        "(spec §10) — the spikes used `uv run --with`, which is not a "
        "production import path"
    )


def test_narrator_module_exposes_query_seam() -> None:
    """``anthropic_sdk_client`` exposes a module-level ``query`` (the OQ-9
    fake-injection seam, mirroring ``build_async_anthropic``)."""
    from sidequest.agents import anthropic_sdk_client

    seam = getattr(anthropic_sdk_client, "query", None)
    assert callable(seam), (
        "anthropic_sdk_client must bind the agent-sdk `query` at module scope "
        "so the narrator tool loop is driven by a late-bound, monkeypatchable "
        "transport (OQ-9) — the entire fake-query test fleet depends on it"
    )


def test_haiku_factory_module_exposes_query_seam() -> None:
    """``llm_factory`` exposes a module-level ``query`` — the four Haiku
    single-shot sites drive the same seam (spec §6.4.4)."""
    from sidequest.agents import llm_factory

    seam = getattr(llm_factory, "query", None)
    assert callable(seam), (
        "llm_factory must bind the agent-sdk `query` at module scope so the "
        "Haiku single-shot sites (router / aside / classifier / archetype) are "
        "driven by a late-bound, monkeypatchable transport (OQ-9)"
    )
