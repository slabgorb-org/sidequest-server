"""Shared test doubles and factories (story 76-9 — consolidated from duplicated copies)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator


class FakeSocket:
    """Minimal WebSocket stand-in that records every broadcast/replay it receives.

    Canonical recording-socket double (story 76-9 — consolidated from 15 copies
    across tests/agents, tests/server, tests/telemetry). Records into ``self.events``.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


def make_orchestrator(*, mock_client: bool = False) -> Orchestrator:
    """Canonical Orchestrator factory (story 76-9 — consolidated from 5 copies).

    ``mock_client=True`` injects a ``MagicMock(spec=ClaudeClient)`` for tests that
    must avoid the real SDK turn loop; the default builds a bare ``Orchestrator()``
    for backend-agnostic prompt-assembly tests.
    """
    if mock_client:
        return Orchestrator(client=MagicMock(spec=ClaudeClient))
    return Orchestrator()
