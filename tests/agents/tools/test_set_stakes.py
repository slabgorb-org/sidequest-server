"""RED tests for the ``set_stakes`` tool — Story 77-2 (ADR-137 Option B).

``set_stakes`` is the narrator-facing affordance to set or append the
session's current stakes (``GameSnapshot.active_stakes``) in ordinary play —
replacing the two off-path writers (the deprecated ``apply_world_patch``
escape hatch and the trope-resolution handshake that never fires in a
prose-only pack). ``ToolCategory.WRITE``, ADR-102 typed contract.

Binding behaviour:
- reuses the existing ``_ACTIVE_STAKES_GUARDRAIL = 1024`` cap so a runaway
  narrator string cannot pollute the next turn's state_summary prompt; the
  trim preserves the freshly-written tail (the load-bearing field).
- fires ``stakes.set`` (length, source, is_fresh) — the GM-panel lie-detector
  for this substrate.

``is_fresh`` semantics (this story's decision; ADR-137 says only "mirrors the
existing ``active_stakes_appended`` flag"): TRUE when this call takes the
stakes field from empty to populated (establishment), FALSE when it evolves
already-present stakes. The oz playtest failure was ``active_stakes: ""`` for
all 13 turns — ``is_fresh=true`` is precisely the signal that the spine's
stakes substrate stopped being empty.
"""

from __future__ import annotations

from typing import Any, cast

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import set_stakes as _set_stakes_module  # noqa: F401
from sidequest.game.session import GameSnapshot, TurnManager
from sidequest.server.narration_apply import _ACTIVE_STAKES_GUARDRAIL


def _build_snapshot(*, active_stakes: str = "") -> GameSnapshot:
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        turn_manager=TurnManager(interaction=1),
        active_stakes=active_stakes,
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, session_id: str = "s", turn: int = 4) -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Dorothy",
        turn_number=turn,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["set_stakes"]  # noqa: SLF001
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_set_stakes_is_registered() -> None:
    assert "set_stakes" in default_registry.list_names()


# --------------------------------------------------------------------------- #
# AC-4 — set / append active_stakes
# --------------------------------------------------------------------------- #


async def test_fresh_stakes_set_on_empty_field() -> None:
    store = _store_with(_build_snapshot(active_stakes=""))
    ctx = _make_ctx(store)

    r = await _call({"stakes": "The Witch will reach Oz by nightfall."}, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.active_stakes == "The Witch will reach Oz by nightfall."


async def test_fresh_stakes_fires_stakes_set_span_is_fresh_true(otel_capture) -> None:
    store = _store_with(_build_snapshot(active_stakes=""))
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-fresh",
            name="set_stakes",
            arguments={"stakes": "The Witch will reach Oz by nightfall."},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "stakes.set"]
    assert spans, f"no stakes.set span; got {[s.name for s in otel_capture.get_finished_spans()]}"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("is_fresh") is True
    assert attrs.get("source") == "narrator"
    assert attrs.get("length") == len("The Witch will reach Oz by nightfall.")


async def test_append_onto_existing_stakes_keeps_both(otel_capture) -> None:
    store = _store_with(_build_snapshot(active_stakes="The road is long."))
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-append",
            name="set_stakes",
            arguments={"stakes": "Now the Witch knows your name.", "append": True},
        ),
        ctx,
    )
    assert out.is_error is False

    reloaded = store.load()
    assert reloaded is not None
    stakes = reloaded.snapshot.active_stakes
    assert "The road is long." in stakes
    assert "Now the Witch knows your name." in stakes

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "stakes.set"]
    assert spans
    # Appending onto already-present stakes is an evolution, not establishment.
    assert dict(spans[-1].attributes or {}).get("is_fresh") is False


async def test_default_mode_replaces_existing_stakes() -> None:
    """Without append, set_stakes overwrites — the narrator re-states the
    current stakes rather than growing the field unbounded."""
    store = _store_with(_build_snapshot(active_stakes="Old stakes."))
    ctx = _make_ctx(store)

    r = await _call({"stakes": "Fresh stakes."}, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.active_stakes == "Fresh stakes."


# --------------------------------------------------------------------------- #
# AC-1 — reuse the 1024 guardrail; trim preserves the fresh tail
# --------------------------------------------------------------------------- #


async def test_guardrail_trims_runaway_append_to_1024_keeping_fresh_tail() -> None:
    assert _ACTIVE_STAKES_GUARDRAIL == 1024  # reuse the existing constant, not a new one
    prior = "P" * 1000
    fresh = "F" * 200  # 1000 + sep + 200 > 1024 -> must trim
    store = _store_with(_build_snapshot(active_stakes=prior))
    ctx = _make_ctx(store)

    r = await _call({"stakes": fresh, "append": True}, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    stored = reloaded.snapshot.active_stakes
    assert len(stored) <= _ACTIVE_STAKES_GUARDRAIL
    # The freshly-written content is the load-bearing tail — it must survive
    # the trim intact (oldest content is dropped, not the new stakes).
    assert stored.endswith(fresh)


# --------------------------------------------------------------------------- #
# Schema discipline (lang-review #11 input validation at the boundary)
# --------------------------------------------------------------------------- #


async def test_empty_stakes_rejected_by_args_model() -> None:
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)
    out = await default_registry.dispatch(
        ToolUseBlock(id="t-empty", name="set_stakes", arguments={"stakes": ""}), ctx
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_oversized_stakes_input_rejected_by_args_model() -> None:
    """A single call cannot exceed the guardrail at the boundary."""
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-huge",
            name="set_stakes",
            arguments={"stakes": "Z" * (_ACTIVE_STAKES_GUARDRAIL + 1)},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    ctx = _make_ctx(pg_empty_store())
    r = await _call({"stakes": "stakes with no session"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message
