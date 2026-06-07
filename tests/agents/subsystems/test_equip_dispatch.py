"""Equip subsystem dispatch tests (NL-equip intent, ADR-113 / Zork-Problem).

The IntentRouter classifies a natural-language equip action ("lace on the
silver shoes", "draw the sword", "take off the cloak") into an ``equip``
dispatch; this handler resolves the named item in the acting PC's inventory
and flips its ``equipped`` flag deterministically (engine-first), emitting an
OTEL span the GM panel can read.

CONTENT-FREE: synthetic Character + inventory dicts, no live genre pack.
Spans are captured via an in-memory OTEL exporter monkeypatched onto
``spans.tracer`` (drive-and-assert, never a source-text grep).
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems import SubsystemOutput, get_registered, run_dispatch_bank
from sidequest.agents.subsystems.equip import run_equip_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Synthetic fixtures (content-free)
# ---------------------------------------------------------------------------


def _item(name: str, *, id: str | None = None, equipped: bool = False, state: str = "Carried") -> dict:
    return {
        "id": id or f"narrator:{name.lower().replace(' ', '_')}",
        "name": name,
        "description": "a thing",
        "category": "armor",
        "equipped": equipped,
        "quantity": 1,
        "state": state,
    }


def _character(name: str = "Susan", items: list[dict] | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="placeholder",
            personality="plucky",
            inventory=Inventory(items=list(items or [])),
        ),
        char_class="Curious Child",
        race="Ordinary-Born",
        backstory="placeholder",
    )


def _snapshot(character: Character) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.characters.append(character)
    return snap


def _dispatch(item: str = "", action: str = "", key: str = "eq1") -> SubsystemDispatch:
    params: dict[str, str] = {"item": item}
    if action:
        params["action"] = action
    return SubsystemDispatch(
        subsystem="equip",
        params=params,
        idempotency_key=key,
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-equip")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _spans_named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _run(coro):
    return asyncio.run(coro)


def _find(snap: GameSnapshot, item_name: str) -> dict:
    items = snap.characters[0].core.inventory.items
    return next(i for i in items if i["name"] == item_name)


# ---------------------------------------------------------------------------
# 1 — equip flips equipped True; state stays Carried; non-error resolved span.
# ---------------------------------------------------------------------------


def test_equip_flips_equipped_true(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=False)])
    snap = _snapshot(char)
    out = _run(
        run_equip_dispatch(
            _dispatch(item="Silver Shoes", action="equip"),
            snapshot=snap,
            player_name="Susan",
        )
    )
    shoes = _find(snap, "Silver Shoes")
    assert shoes["equipped"] is True
    # state is the Carried/Discarded axis, NOT the equip axis — must stay
    # Carried so the item remains in the carried/equipment view (views.py).
    assert shoes["state"] == "Carried"
    assert out.data["equipped"] is True
    assert out.data["changed"] is True
    resolved = _spans_named(capture_spans, "equip.resolved")
    assert len(resolved) == 1
    assert resolved[0].attributes["pc_name"] == "Susan"
    assert resolved[0].attributes["item_name"] == "Silver Shoes"
    assert resolved[0].attributes["equipped_after"] is True
    assert resolved[0].attributes["changed"] is True
    assert resolved[0].status.status_code != StatusCode.ERROR
    assert not _spans_named(capture_spans, "equip.unresolved")


# ---------------------------------------------------------------------------
# 2 — default action (no action param) is equip ("lace on the shoes").
# ---------------------------------------------------------------------------


def test_equip_defaults_to_equip_when_action_omitted(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=False)])
    snap = _snapshot(char)
    out = _run(
        run_equip_dispatch(_dispatch(item="Silver Shoes"), snapshot=snap, player_name="Susan")
    )
    assert _find(snap, "Silver Shoes")["equipped"] is True
    assert out.data["equipped"] is True


# ---------------------------------------------------------------------------
# 3 — unequip flips equipped False.
# ---------------------------------------------------------------------------


def test_unequip_flips_equipped_false(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=True)])
    snap = _snapshot(char)
    out = _run(
        run_equip_dispatch(
            _dispatch(item="Silver Shoes", action="unequip"),
            snapshot=snap,
            player_name="Susan",
        )
    )
    assert _find(snap, "Silver Shoes")["equipped"] is False
    assert out.data["equipped"] is False
    assert out.data["changed"] is True


# ---------------------------------------------------------------------------
# 4 — item matched by name, case-insensitive ("silver shoes" → "Silver Shoes").
# ---------------------------------------------------------------------------


def test_equip_matches_name_case_insensitive(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=False)])
    snap = _snapshot(char)
    out = _run(
        run_equip_dispatch(_dispatch(item="silver shoes"), snapshot=snap, player_name="Susan")
    )
    assert _find(snap, "Silver Shoes")["equipped"] is True
    assert out.data["matched_by"] in ("name", "substring")


# ---------------------------------------------------------------------------
# 5 — item matched by id.
# ---------------------------------------------------------------------------


def test_equip_matches_by_id(capture_spans):
    char = _character(items=[_item("Silver Shoes", id="narrator:silver_shoes")])
    snap = _snapshot(char)
    _run(
        run_equip_dispatch(
            _dispatch(item="narrator:silver_shoes"), snapshot=snap, player_name="Susan"
        )
    )
    assert _find(snap, "Silver Shoes")["equipped"] is True


# ---------------------------------------------------------------------------
# 6 — item the PC isn't carrying → fail loud (item_not_found), no mutation.
# ---------------------------------------------------------------------------


def test_equip_item_not_found_fails_loud(capture_spans):
    char = _character(items=[_item("Sensible Shoes", equipped=False)])
    snap = _snapshot(char)
    out = _run(
        run_equip_dispatch(_dispatch(item="Ruby Slippers"), snapshot=snap, player_name="Susan")
    )
    assert out.data["error"] == "item_not_found"
    # No item was mutated.
    assert _find(snap, "Sensible Shoes")["equipped"] is False
    # Honest narrator surface directive (the PC is told the truth).
    assert out.directives and out.directives[0].kind == "must_narrate"
    unresolved = _spans_named(capture_spans, "equip.unresolved")
    assert unresolved[0].attributes["reason"] == "item_not_found"
    assert unresolved[0].status.status_code == StatusCode.ERROR
    assert not _spans_named(capture_spans, "equip.resolved")


# ---------------------------------------------------------------------------
# 7 — acting PC not among characters → fail loud (no_character).
# ---------------------------------------------------------------------------


def test_equip_no_character_fails_loud(capture_spans):
    snap = _snapshot(_character(name="Susan", items=[_item("Silver Shoes")]))
    out = _run(
        run_equip_dispatch(_dispatch(item="Silver Shoes"), snapshot=snap, player_name="Nobody")
    )
    assert out.data["error"] == "no_character"
    unresolved = _spans_named(capture_spans, "equip.unresolved")
    assert unresolved[0].attributes["reason"] == "no_character"


# ---------------------------------------------------------------------------
# 8 — idempotent: equipping an already-equipped item → resolved, changed False.
# ---------------------------------------------------------------------------


def test_equip_idempotent_already_equipped(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=True)])
    snap = _snapshot(char)
    out = _run(
        run_equip_dispatch(
            _dispatch(item="Silver Shoes", action="equip"), snapshot=snap, player_name="Susan"
        )
    )
    assert _find(snap, "Silver Shoes")["equipped"] is True
    assert out.data["changed"] is False
    resolved = _spans_named(capture_spans, "equip.resolved")
    assert resolved[0].attributes["changed"] is False


# ---------------------------------------------------------------------------
# 9 — wiring: registry includes equip → run_equip_dispatch.
# ---------------------------------------------------------------------------


def test_wiring_registry_includes_equip():
    assert get_registered().get("equip") is run_equip_dispatch


# ---------------------------------------------------------------------------
# 10 — wiring: equip dispatch flows through the bank → flag flips + span.
# ---------------------------------------------------------------------------


def test_wiring_bank_invokes_equip(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=False)])
    snap = _snapshot(char)
    package = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="Susan",
                raw_action="lace on the silver shoes",
                dispatch=[_dispatch(item="Silver Shoes", action="equip")],
            )
        ],
        confidence_global=0.9,
    )
    _run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "player_name": "Susan",
                "npcs_present": [],
            },
        )
    )
    assert _find(snap, "Silver Shoes")["equipped"] is True
    assert _spans_named(capture_spans, "equip.resolved")


# ---------------------------------------------------------------------------
# Sanity: success returns no narrator directive (engine truth is the flag).
# ---------------------------------------------------------------------------


def test_equip_success_returns_no_directives(capture_spans):
    char = _character(items=[_item("Silver Shoes", equipped=False)])
    snap = _snapshot(char)
    out: SubsystemOutput = _run(
        run_equip_dispatch(_dispatch(item="Silver Shoes"), snapshot=snap, player_name="Susan")
    )
    assert out.directives == []
