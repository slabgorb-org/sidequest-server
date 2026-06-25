"""WWN combat must SEAT, not narrate-only (story 158-28, ADR-143/113/116/006).

sq-playtest pingpong 2026-06-24 beneath_sunden + 2026-06-25 barsoom: a player
attacks a creature in a WWN world, the narrator narrates a KILL, but ground truth
shows ``encounter=null`` / ``total_beats_fired=0`` / creature untouched — combat
is NARRATION-ONLY. #1072 stopped the crash; it did not make combat SEAT.

ROOT CAUSE (proven by a live IntentRouter probe against the real heavy_metal
pack, 2026-06-25 — see story 158-28 session notes):

  * Blunt combat verbs — "I attack the banth with my long-sword." — make the
    LIVE router emit ``subsystem="combat"``. But ``combat`` is NOT a registered
    subsystem (the registered engager is ``confrontation``; "combat" is only a
    confrontation *type*, ``params["type"]``). ``run_dispatch_bank`` looks up
    ``combat`` in the registry, finds nothing, logs ``subsystems.unknown
    subsystem=combat`` and **silently continues** — no engine engages, nothing
    seats, and the narrator then confabulates a kill over an empty encounter.
  * Literary phrasing — "I lunge ... driving my long-sword at its throat." —
    happens to route to ``subsystem="confrontation"`` and seats cleanly. THE
    OPPONENT SEATER IS NOT BROKEN: given a ``confrontation`` dispatch it seats
    the encounter, mints the Other, and rolls initiative (proven in the
    regression guard below). The defect is the router→registry vocabulary
    mismatch plus the bank's silent drop of an unhandled combat classification
    (a No-Silent-Fallbacks violation — the misclassification becomes a phantom
    kill instead of a loud signal).

These tests reproduce the bug DETERMINISTICALLY by feeding the EXACT live-router
output (``subsystem="combat"``) through the production ``run_dispatch_bank`` —
no LLM in the test. The router-prompt half of the fix (never emit ``combat``) is
validated by opt-in live eval; this deterministic suite pins the END-STATE
contract (AC1: an attack SEATS) and the bank-robustness safety net so a future
misclassification can never again silently degrade to narration-only.

AC1  — after an attack, ``snapshot.encounter is not None`` (a confrontation seats).
AC5  — a combat seat whose initiative cannot resolve degrades LOUDLY (ADR-006),
       it does not propagate an uncaught exception that wedges the turn.
"""

from __future__ import annotations

import pytest

import sidequest.agents.subsystems.confrontation  # noqa: F401 — registers run_confrontation_dispatch
from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef, RulesConfig, WwnConfig
from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch, SubsystemDispatch

# The full WWN ability map (heavy_metal uses the canonical abbreviations).
_WN_ATTRIBUTE_MAP = {
    "STRENGTH": "STR",
    "DEXTERITY": "DEX",
    "CONSTITUTION": "CON",
    "INTELLIGENCE": "INT",
    "WISDOM": "WIS",
    "CHARISMA": "CHA",
}
_PLAYER = "Tars"
_OPPONENT = "Banth"


def _wn_combat_cdef() -> ConfrontationDef:
    """A WWN ``hp_depletion`` combat def — opponent_default_stats carries the six
    ability scores plus the reserved hp/armor_class/dexterity the seater seeds
    and the initiative roll reads (mirrors heavy_metal rules.yaml 'Blade-work')."""
    return ConfrontationDef(
        type="combat",
        label="Blade-work",
        category="combat",
        win_condition="hp_depletion",  # type: ignore[arg-type]
        opponent_default_stats={
            "STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10,
            "hp": 10, "armor_class": 12, "dexterity": 11,
        },
    )


def _wn_pack() -> GenrePack:
    from unittest.mock import MagicMock

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_WN_ATTRIBUTE_MAP.values()),
        wwn=WwnConfig(attribute_map=_WN_ATTRIBUTE_MAP),
        confrontations=[_wn_combat_cdef()],
    )
    pack.effective_cultures.return_value = ([], "genre")
    pack.source_dir = None
    pack.worlds = {}
    return pack


def _statted_player(name: str = _PLAYER) -> Character:
    core = CreatureCore(
        name=name,
        description="A red-Martian swordsman of Helium",
        personality="bold",
        inventory=Inventory(items=[{"name": "long-sword", "state": "Wielded", "quantity": 1}]),
        hp={"current": 20, "max": 20, "base_max": 20},
        armor_class=14,
    )
    return Character(
        core=core,
        char_class="Fighter",
        race="Red Martian",
        backstory="Helium-born.",
        stats={"STR": 14, "DEX": 13, "CON": 12, "INT": 10, "WIS": 11, "CHA": 12},
    )


def _barsoom_like_snapshot(player: Character) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="barsoom",
        turn_manager=TurnManager(interaction=3),
    )
    snap.characters.append(player)
    snap.character_locations[player.core.name] = "helium"
    # A banth on-stage as a promoted NPC — the Other the player attacks.
    banth_core = CreatureCore(
        name=_OPPONENT,
        description="A great ten-legged lion of the dead sea bottoms",
        personality="ferocious",
        inventory=Inventory(items=[]),
        hp={"current": 27, "max": 27, "base_max": 27},
        armor_class=14,
    )
    snap.npcs.append(Npc(core=banth_core))
    snap.character_locations[_OPPONENT] = "helium"
    return snap


def _combat_package(subsystem: str) -> DispatchPackage:
    """A one-dispatch package mirroring the router's output for a combat verb.

    ``subsystem`` is the variable under test: the live router emits ``"combat"``
    for a blunt "I attack X" (the bug) and ``"confrontation"`` for literary
    phrasing (the working path). params carry the confrontation TYPE + the
    router-named Other (ADR-116), exactly as the live probe recorded.
    """
    return DispatchPackage(
        turn_id="turn-3",
        confidence_global=0.95,
        per_player=[
            PlayerDispatch(
                player_id=f"player:{_PLAYER}",
                raw_action="I attack the banth with my long-sword.",
                dispatch=[
                    SubsystemDispatch(
                        subsystem=subsystem,
                        idempotency_key="turn-3:combat:0",
                        confidence=0.95,
                        params={
                            "type": "combat",
                            "opponent": {"name": _OPPONENT, "description": "a great banth"},
                        },
                    )
                ],
            )
        ],
    )


def _context(snap: GameSnapshot, pack: GenrePack) -> dict:
    return {
        "snapshot": snap,
        "pack": pack,
        "player_name": _PLAYER,
        "npcs_present": [],
        "turn_number": 3,
    }


# ---------------------------------------------------------------------------
# AC1 — the bug: a blunt combat verb (router subsystem="combat") must SEAT.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blunt_attack_combat_classification_seats_a_confrontation(otel_capture) -> None:
    """AC1 (RED): the live router emits ``subsystem="combat"`` for "I attack the
    banth ..."; the dispatch bank must SEAT a confrontation for it, not silently
    drop it. Ground truth: ``snapshot.encounter is not None`` with the banth as
    the seated Other, and the existing ``encounter.confrontation_initiated``
    lie-detector span fires (one mechanism per problem — no new 'seated' span).

    Today the bank looks up ``combat`` in the registry, finds nothing, and
    continues — the encounter never seats and the narrator confabulates a kill.
    """
    snap = _barsoom_like_snapshot(_statted_player())
    pack = _wn_pack()

    result = await run_dispatch_bank(_combat_package("combat"), context=_context(snap, pack))

    assert snap.encounter is not None, (
        "a blunt combat verb (router subsystem='combat') must SEAT a confrontation "
        "— the bank silently dropped the unhandled 'combat' classification, leaving "
        "encounter=None for the narrator to confabulate a kill over (158-28 root cause)"
    )
    opponents = [a.name for a in snap.encounter.actors if a.side == "opponent"]
    assert _OPPONENT in opponents, (
        f"the router-named Other must be seated as the opponent; got actors="
        f"{[(a.name, a.side) for a in snap.encounter.actors]}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "encounter.confrontation_initiated" in span_names, (
        "seating MUST fire the existing encounter.confrontation_initiated lie-detector "
        f"span so the GM panel proves the engine engaged; got spans={span_names}"
    )
    # The classification must not be silently recorded as an unhandled no-op.
    assert not any(d.get("decision") == "unknown_subsystem" for d in result.decisions), (
        "a combat-classified action must never be recorded as 'unknown_subsystem' and "
        "dropped (No Silent Fallbacks) — it must reach the confrontation engine"
    )


# ---------------------------------------------------------------------------
# Regression guard (PASSES today) — the opponent seater itself is NOT broken.
# Proves the fix target is the router→registry vocabulary, not the seater.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confrontation_classification_seats_cleanly_regression_guard(otel_capture) -> None:
    """Literary phrasing routes to ``subsystem="confrontation"`` and seats today.
    This guard pins that the seater stays healthy: the fix must not regress the
    path that already works (ADR-143 — bind, don't rebuild)."""
    snap = _barsoom_like_snapshot(_statted_player())
    pack = _wn_pack()

    await run_dispatch_bank(_combat_package("confrontation"), context=_context(snap, pack))

    assert snap.encounter is not None, "the confrontation path must seat (it does today)"
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "encounter.confrontation_initiated" in span_names
    assert "encounter.initiative_rolled" in span_names, (
        "a statted player seats WITH initiative — proving the seater is whole"
    )
    # AC2/AC3 precondition: the seated Other carries an ablatable HP pool reachable
    # by the WN round — so once AC1 routes blunt attacks here too, beats fire and
    # HP changes through the existing state_patch.hp / encounter.beat_applied path
    # (no new spans needed — one mechanism per problem).
    opp_core = snap.find_creature_core(_OPPONENT)
    assert opp_core is not None and opp_core.hp.current >= 1, (
        "the seated opponent must have an HP pool reachable via find_creature_core "
        "(AC3 precondition: without it the WN round has no defender HP to ablate)"
    )


# ---------------------------------------------------------------------------
# AC5 — a combat seat whose initiative cannot resolve degrades LOUDLY (ADR-006),
# never propagating an uncaught exception that wedges the turn. The repro showed
# a player missing a DEX stat makes the initiative roll raise an uncaught
# ValueError that run_confrontation_dispatch does NOT catch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combat_seat_with_unresolvable_initiative_degrades_loudly() -> None:
    """AC5 (RED): seating a WWN combat for a player whose stat block lacks DEX
    must degrade LOUDLY — the dispatch returns an error outcome and the turn
    survives — NOT raise an uncaught ValueError from inside the confrontation
    handler. Today the handler only catches NoOpponentAvailableError /
    SealedLetterArityError, so the initiative ValueError escapes it."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch

    statless = _statted_player("Dotar")
    statless.stats = {}  # a player with no ability scores (the reproduced crash input)
    snap = _barsoom_like_snapshot(statless)
    pack = _wn_pack()

    dispatch = SubsystemDispatch(
        subsystem="confrontation",
        idempotency_key="turn-3:combat:0",
        confidence=0.95,
        params={"type": "combat", "opponent": {"name": _OPPONENT}},
    )

    # Must NOT raise an uncaught ValueError out of the handler (ADR-006).
    try:
        out = await run_confrontation_dispatch(
            dispatch,
            snapshot=snap,
            pack=pack,
            player_name="Dotar",
            npcs_present=[NpcMention(name=_OPPONENT, side="opponent")],
        )
    except ValueError as exc:  # pragma: no cover - this is the RED failure
        pytest.fail(
            "combat seating must degrade loudly when initiative cannot resolve "
            f"(ADR-006), not raise an uncaught ValueError from the handler: {exc!r}"
        )

    assert out is not None and out.data.get("error"), (
        "an unresolvable-initiative seat must surface a loud error outcome on the "
        "SubsystemOutput so the GM panel sees the degradation, not a silent failure"
    )
