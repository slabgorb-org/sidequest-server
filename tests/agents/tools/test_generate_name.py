"""Tests for the generate_name tool — Phase C Task 24.

GENERATE tool. Wraps :class:`~sidequest.genre.names.generator.NameGenerator`
per ADR-091. Phase B amendment #4 adds
``name_generators: dict[str, NameGenerator] | None`` to
:class:`ToolContext`. Tests cover the wired path (with a hand-built
NameGenerator using word_list slots — no corpus I/O needed) and the
unwired path (the OTEL marker no-op).
"""

from __future__ import annotations

import random
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import generate_name as _generate_name_module  # noqa: F401
from sidequest.game.persistence import SqliteStore
from sidequest.genre.names.generator import NameGenerator, SlotGenerator

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _store() -> SqliteStore:
    s = SqliteStore.open_in_memory()
    s.initialize()
    return s


def _make_ctx(
    *,
    name_generators: dict[str, NameGenerator] | None = None,
    store: SqliteStore | None = None,
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Alice",
        turn_number=1,
        store=store if store is not None else _store(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        name_generators=name_generators,
    )


def _build_namegen(
    *,
    given: list[str] | None = None,
    surname: list[str] | None = None,
    tavern: list[str] | None = None,
    person_patterns: list[str] | None = None,
    place_patterns: list[str] | None = None,
    seed: int = 42,
) -> NameGenerator:
    """Hand-build a NameGenerator using word_list slots (no corpus I/O).

    SlotGenerator with chain=None and a populated word_list returns a
    random element of the list on each .generate() call — perfectly
    deterministic given a seeded RNG, which keeps the tests stable.
    """
    rng = random.Random(seed)
    slots: dict[str, SlotGenerator] = {}
    if given is not None:
        slots["given_name"] = SlotGenerator(chain=None, word_list=given, rng=rng)
    if surname is not None:
        slots["surname"] = SlotGenerator(chain=None, word_list=surname, rng=rng)
    if tavern is not None:
        slots["tavern_name"] = SlotGenerator(chain=None, word_list=tavern, rng=rng)
    return NameGenerator(
        slots=slots,
        person_patterns=list(person_patterns or []),
        place_patterns=list(place_patterns or []),
        rng=rng,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["generate_name"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _otel(ctx: ToolContext) -> dict[str, Any]:
    span = cast(MagicMock, ctx.otel_span)
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_generate_name_is_registered() -> None:
    assert "generate_name" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Unwired (name_generators=None) path
# ---------------------------------------------------------------------------


async def test_unwired_returns_empty_with_marker() -> None:
    ctx = _make_ctx(name_generators=None)
    r = await _call({"culture": "Surface Folk", "kind": "given", "count": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["names"] == []
    assert p["name_generators_wired"] is False
    assert p["culture"] == "Surface Folk"
    assert p["kind"] == "given"

    recorded = _otel(ctx)
    assert recorded["tool.namegen.culture"] == "Surface Folk"
    assert recorded["tool.namegen.kind"] == "given"
    assert recorded["tool.namegen.count"] == 0
    assert recorded["tool.namegen.name_generators_wired"] is False


# ---------------------------------------------------------------------------
# Wired path — happy cases
# ---------------------------------------------------------------------------


async def test_wired_given_name_returns_count_names() -> None:
    ng = _build_namegen(given=["Bram", "Cob", "Durn", "Edric", "Falk"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "given", "count": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["name_generators_wired"] is True
    assert p["culture"] == "Surface Folk"
    assert p["kind"] == "given"
    assert len(p["names"]) == 3
    # Every emitted name must come from the slot's word_list.
    assert all(n in {"Bram", "Cob", "Durn", "Edric", "Falk"} for n in p["names"])


async def test_wired_family_resolves_to_surname_slot() -> None:
    ng = _build_namegen(
        given=["Bram"],
        surname=["Blackhand", "Ironjaw", "Stonehelm"],
    )
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "family", "count": 2}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert len(p["names"]) == 2
    assert all(n in {"Blackhand", "Ironjaw", "Stonehelm"} for n in p["names"])


async def test_wired_place_uses_place_patterns() -> None:
    ng = _build_namegen(
        given=["Aldric"],
        surname=["Blackhand"],
        person_patterns=["{given_name} {surname}"],
        place_patterns=["{surname} Square", "The {given_name}'s Rest"],
    )
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "place", "count": 4}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert len(p["names"]) == 4
    # Place names must come from the place_patterns templates filled with
    # the only available slot tokens.
    assert all(name == "Blackhand Square" or name == "The Aldric's Rest" for name in p["names"])


async def test_count_caps_at_ten() -> None:
    ng = _build_namegen(given=["Bram", "Cob"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "given", "count": 10}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert len(p["names"]) == 10

    recorded = _otel(ctx)
    assert recorded["tool.namegen.count"] == 10


async def test_otel_attrs_full_on_success() -> None:
    ng = _build_namegen(given=["Bram", "Cob"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "given", "count": 2}, ctx)
    assert r.status is ToolResultStatus.OK

    recorded = _otel(ctx)
    assert recorded["tool.namegen.culture"] == "Surface Folk"
    assert recorded["tool.namegen.kind"] == "given"
    assert recorded["tool.namegen.count"] == 2
    assert recorded["tool.namegen.name_generators_wired"] is True


# ---------------------------------------------------------------------------
# not_found paths
# ---------------------------------------------------------------------------


async def test_unknown_culture_returns_not_found_with_available_list() -> None:
    ng = _build_namegen(given=["Bram"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng, "Keeper Titles": ng})

    r = await _call({"culture": "Nonesuch", "kind": "given"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Nonesuch" in r.message
    # The available cultures should appear in the message for narrator
    # self-correction.
    assert "Keeper Titles" in r.message
    assert "Surface Folk" in r.message

    recorded = _otel(ctx)
    assert recorded["tool.namegen.count"] == 0
    assert recorded["tool.namegen.name_generators_wired"] is True


async def test_unknown_kind_returns_not_found_with_available_slots() -> None:
    # Culture has only given_name + surname. Asking for kind='tavern' must
    # surface the available slot list so the narrator can re-call.
    ng = _build_namegen(given=["Bram"], surname=["Blackhand"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "tavern"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "tavern" in r.message
    # Available slots are listed for the narrator.
    assert "given_name" in r.message
    assert "surname" in r.message

    recorded = _otel(ctx)
    assert recorded["tool.namegen.count"] == 0
    assert recorded["tool.namegen.name_generators_wired"] is True


async def test_place_without_place_patterns_returns_not_found() -> None:
    ng = _build_namegen(given=["Bram"], place_patterns=None)
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    r = await _call({"culture": "Surface Folk", "kind": "place"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "place" in r.message
    # Slot list still surfaces for forward-correction.
    assert "given_name" in r.message


# ---------------------------------------------------------------------------
# Validator errors
# ---------------------------------------------------------------------------


async def test_empty_culture_is_validator_error() -> None:
    ng = _build_namegen(given=["Bram"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="generate_name",
            arguments={"culture": "", "kind": "given"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_count_above_ten_is_validator_error() -> None:
    ng = _build_namegen(given=["Bram"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-eleven",
            name="generate_name",
            arguments={"culture": "Surface Folk", "kind": "given", "count": 11},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_count_zero_is_validator_error() -> None:
    ng = _build_namegen(given=["Bram"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-zero",
            name="generate_name",
            arguments={"culture": "Surface Folk", "kind": "given", "count": 0},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_invalid_kind_is_validator_error() -> None:
    ng = _build_namegen(given=["Bram"])
    ctx = _make_ctx(name_generators={"Surface Folk": ng})

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-kind",
            name="generate_name",
            arguments={"culture": "Surface Folk", "kind": "potato"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content
