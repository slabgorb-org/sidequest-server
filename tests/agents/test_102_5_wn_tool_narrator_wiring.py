"""Story 102-5 (AC4) — WN tool contract wired through the production narrator
tool-dispatch path.

§12: tool-level isolation is necessary but not sufficient — at least one test
drives the production tool walk that invokes a WN tool and asserts the span
chain (narrator → tool → module → OTEL).

Story 119-3: the narrator transport ported to ``claude-agent-sdk``, whose
``query()`` loop OWNS tool invocation — it calls the in-process SDK-MCP ``@tool``
handler the narrator client builds. That handler is
``_build_narration_tool_handler`` → the orchestrator's ``tool_dispatch`` closure
→ the REAL ``default_registry.dispatch``. A hermetic fake ``query`` cannot drive
the SDK's handler invocation (the SDK owns the mcp transport — covered by
``test_119_3_narrator_port.py``), so this test drives the EXACT production
handler the SDK invokes, against the REAL registry dispatch over a real
Postgres-backed store. Nothing tool-side is mocked: this is the production tool
walk. The advertised toolset is also asserted to expose ``wn_attack`` under the
production ``mcp__narration__`` allowed-tools shape ``complete_with_tools``
builds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

import sidequest.agents.tools  # noqa: F401  (production registration path)
from sidequest.agents.anthropic_sdk_client import (
    AnthropicSdkClient,
    _build_narration_tool_handler,
)
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import (
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import WwnConfig

# --- WWN fixture world -------------------------------------------------------

_AMAP = {
    a: a for a in ("STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA")
}


@dataclass
class _FakeRules:
    ruleset: str = "wwn"
    _cfg: Any = None

    def __post_init__(self) -> None:
        if self._cfg is None:
            self._cfg = WwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> Any:
        return self._cfg


@dataclass
class _FakePack:
    rules: _FakeRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _FakeRules()


def _pc(name: str, *, ac: int = 10, hp: int = 10, items: list[dict] | None = None) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=items or []),
        hp=HpPool(current=hp, max=hp, base_max=hp),
        armor_class=ac,
    )
    return Character(
        core=core,
        backstory="b",
        char_class="Warrior",
        race="Human",
        stats={a: 14 for a in _AMAP},
    )


@pytest.mark.asyncio
async def test_narrator_turn_drives_wn_attack_through_production_dispatch(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """AC4: the production tool walk invokes ``wn_attack`` through the EXACT
    ``@tool`` handler the agent-SDK ``query()`` loop calls
    (``_build_narration_tool_handler`` → the dispatch closure → real
    ``default_registry.dispatch``, real store) and the full span chain fires:
    ``tool.{cat}.wn_attack`` (registry, by construction) AND
    ``wwn.attack.resolved`` (module, slug-honest) — the engine-adjudicated
    result both persists (HpPool delta) and is returned to the model in the SDK
    handler-reply shape (``"hit": true``) the narrator must describe FROM, not
    improvise."""
    import re

    from tests.agents.tools.conftest import pg_store_with

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    attacker = _pc(
        "Vesska",
        items=[{"id": "shard-knife", "name": "Shard Knife", "damage": {"dice": "1d6", "bonus": 0}}],
    )
    target = _pc("Husk", ac=-100, hp=10)  # forced hit
    snapshot = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="long_foundry",
        turn_manager=TurnManager(interaction=1),
        characters=[attacker, target],
        npcs=[],
    )
    store = pg_store_with(snapshot)
    pack = _FakePack()
    ctx = ToolContext(
        world_id="w",
        session_id="wn-wiring",
        perspective_pc="Vesska",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=pack,
    )

    async def _dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return await default_registry.dispatch(block, ctx)

    # The PRODUCTION advertisement shape: the wwn-bound narrator's tool list,
    # and the SDK-MCP allowed-tools the narrator client builds from it.
    tools = default_registry.tool_definitions(ruleset="wwn")
    assert any(t.name == "wn_attack" for t in tools), (
        "wn_attack missing from the wwn-bound narrator's advertised toolset"
    )
    _mcp_servers, allowed_tools = AnthropicSdkClient()._build_narration_mcp(tools, _dispatch, [])
    assert "mcp__narration__wn_attack" in allowed_tools, (
        "wn_attack missing from the SDK-MCP allowed_tools complete_with_tools advertises"
    )

    # Drive the EXACT @tool handler the SDK invokes for wn_attack (spec §5.3).
    accumulator: list[ToolUseBlock] = []
    handler = _build_narration_tool_handler(
        bare_name="wn_attack", tool_dispatch=_dispatch, accumulator=accumulator
    )
    sdk_reply = await handler({"attacker": "Vesska", "target": "Husk", "weapon": "Shard Knife"})

    # The engine's adjudication is the SDK handler reply fed back to the model —
    # the narrator describes it, it does not invent one. The reply content is the
    # ToolResultBlock content (a JSON string), carrying the unescaped "hit": true.
    assert sdk_reply["is_error"] is False
    tr_content = sdk_reply["content"][0]["text"]
    assert '"hit": true' in tr_content, (
        f"the engine's hit adjudication never reached the narrator: {tr_content!r}"
    )
    # The call is accumulated onto the per-turn ledger (GM-panel lie-detector).
    assert [b.name for b in accumulator] == ["wn_attack"]

    # Span chain: narrator → tool (registry, by construction) → module (slug-honest).
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert any(re.fullmatch(r"tool\.(read|write|gen)\.wn_attack", n) for n in names), (
        f"no registry dispatch span for wn_attack; got {names}"
    )
    assert "wwn.attack.resolved" in names, (
        f"no wwn.attack.resolved module span — the GM panel cannot verify the attack; got {names}"
    )
    assert not any(n == "tool.unknown.wn_attack" for n in names), (
        "wn_attack hit the unknown-tool path — the contract is not registered"
    )

    # The HpPool delta persisted through the production write path.
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current < 10, "the forced hit's damage never landed on the persisted HpPool"
