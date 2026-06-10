"""RED test for Story 102-5 (AC4) — WN tool contract wired through the
production narrator tool-dispatch path.

§12: tool-level isolation is necessary but not sufficient — at least one test
drives a REAL narrator turn that invokes a WN tool through the production
dispatch path and asserts the span chain (narrator → tool → module → OTEL).

Pattern: the 82-9 / 71-40 fake-SDK-transport harness. The REAL
``AnthropicSdkClient.complete_with_tools`` loop runs against a scripted SDK
transport whose first response is a ``wn_attack`` tool_use block; the
``tool_dispatch`` callable is the REAL ``default_registry.dispatch`` over a
real Postgres-backed store. Nothing narrator-side is mocked below the SDK
transport seam — this is the production tool walk.

RED: ``wn_attack`` is not registered, so the dispatch returns the
``tool.unknown.wn_attack`` error result and no module span fires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

import sidequest.agents.tools  # noqa: F401  (production registration path)
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import WwnConfig

# --- minimal SDK-shape fakes (the 71-40 / 82-9 pattern) ---------------------


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: Any = None


@dataclass(frozen=True)
class _TextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _ToolUseBlockShape:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _SdkResponse:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _FakeMessages:
    def __init__(self, responses: list[_SdkResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _SdkResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeMessages: out of scripted responses")
        return self._responses.pop(0)


class _FakeSdk:
    def __init__(self, responses: list[_SdkResponse]) -> None:
        self.messages = _FakeMessages(responses)


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


def _attack_tool_use() -> _SdkResponse:
    return _SdkResponse(
        content=[
            _ToolUseBlockShape(
                type="tool_use",
                id="t-attack",
                name="wn_attack",
                input={"attacker": "Vesska", "target": "Husk", "weapon": "Shard Knife"},
            )
        ],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=10, output_tokens=2),
        model="claude-sonnet-4-6",
    )


def _narration() -> _SdkResponse:
    return _SdkResponse(
        content=[_TextBlock(type="text", text="The shard-knife bites deep.")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=12, output_tokens=6),
        model="claude-sonnet-4-6",
    )


@pytest.mark.asyncio
async def test_narrator_turn_drives_wn_attack_through_production_dispatch(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """AC4: a real narrator tool walk (real ``complete_with_tools`` loop, real
    ``default_registry.dispatch``, real store) invokes ``wn_attack`` and the
    full span chain fires: ``tool.{cat}.wn_attack`` (registry, by
    construction) AND ``wwn.attack.resolved`` (module, slug-honest) — and the
    engine-adjudicated result both persists (HpPool delta) and returns to the
    model as a tool_result the narrator must describe FROM, not improvise."""
    import re

    from tests.agents.tools.conftest import pg_store_with

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

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

    # The PRODUCTION advertisement shape: the wwn-bound narrator's tool list.
    tools = default_registry.tool_definitions(ruleset="wwn")
    assert any(t.name == "wn_attack" for t in tools), (
        "wn_attack missing from the wwn-bound narrator's advertised toolset"
    )

    fake = _FakeSdk(responses=[_attack_tool_use(), _narration()])
    client = AnthropicSdkClient(sdk=fake)
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="You narrate WN combat through tools.", cache=True)],
        messages=[Message(role="user", content="Vesska: I drive the shard-knife into the husk")],
        tools=tools,
        tool_dispatch=_dispatch,
        model="claude-sonnet-4-6",
        max_iterations=4,
    )

    # The tool result went BACK to the model (the narrator describes the
    # engine's adjudication — it does not invent one).
    assert len(fake.messages.calls) == 2, "expected tool round-trip then narration"
    second_call = json.dumps(fake.messages.calls[1], default=str)
    assert "tool_result" in second_call
    assert '"hit": true' in second_call.replace("'", '"') or '"hit": True' in second_call, (
        f"the engine's hit adjudication never reached the narrator: {second_call[:400]}"
    )

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
