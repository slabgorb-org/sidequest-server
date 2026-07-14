"""RED (rework round 3) — Story 166-10 (ADR-156 §6): the stage name must reach
EVERY surface that shows the player a seated actor, not just the one in the diff.

WHY THIS FILE EXISTS
--------------------
This story has been rejected twice, and both rejections are the same mistake at
different depths.

  * Round 1: I pinned the prose name onto ``EncounterActor.name`` — the entity id.
    The enemy became impossible to hit. The test I owed and didn't write was one
    sentence long: *rename the enemy, then attack it.*
  * Round 2: the design was corrected (id stays ``name``, label lands on the new
    ``display_name``) and I followed ``display_name`` out to ONE consumer —
    ``ConfrontationOverlay`` — and stopped. There are THREE. The Fate conflict
    panel and the tactical battle map each read the same ``EncounterActor`` through
    their own projection, and each drops the label on the floor.

The lesson was never "fix ``find_creature_core``" or "fix the overlay". It is:
**follow the field out of the diff, all the way, every branch.** So this file starts
from the field and enumerates its consumers, rather than starting from the diff.

THREE PROJECTIONS READ ``EncounterActor``. ONLY ONE HONORS THE LABEL.
--------------------------------------------------------------------
=========================  ==================================  ================
projection                  player surface                      carries label?
=========================  ==================================  ================
``build_confrontation_      ``ConfrontationOverlay``             YES — actor
payload`` (model_dump)      (WN / native packs)                  ``model_dump()``
``_project_conflict_        ``FateConflictSurface``              **NO** — drops it
participant``               (pulp_noir, spaghetti_western,       (``fate_projection
                            tea_and_murder, wry_whimsy)          .py:155``)
``_place_tokens_on_         ``TacticalGridRenderer``             **NO** — feeds the
anchors``                   (battle map, any pack)               seat id into
                                                                 ``label``
=========================  ==================================  ================

``_attach_before_mint`` has **no ruleset gate** — the promotion fires for any
encounter with a lone GENERIC/NARRATOR_INVENTED Other, Fate included. So on four of
eleven live packs the server does the promotion work, emits
``green_room.actor_promoted`` asserting it happened, and the projection throws the
result away. The panel — *including the dropdown the player clicks to choose who to
attack* — keeps showing the coal name forever, while the narration says the proper
one. That is this story's bug, verbatim, unfixed. The GM panel's lie detector would
itself be lying, which is strictly worse than never having promoted at all.

The battle-map token is the same bug a third time, and it is the most galling one:
``TokenPayload`` **already has a clean id/label split** (``token_id`` vs ``label``)
and ``map_emit`` feeds the seat id into ``label`` anyway.

WHAT THIS FILE PINS
-------------------
One rule, applied to every surface:

    **Every field the player READS shows ``display_name ?? name``.
    Every field the engine RESOLVES BY stays the canonical seat id.**

Tests 1-6 walk that rule across the two broken projections, asserting BOTH halves
each time — the label the player sees, and the id the click sends back. A fix that
renames the id to get the label right fails the second assertion, which is exactly
how round 1 died.

Tests 7-8 kill the fold-collision the widening introduced: ``find_creature_core``
now resolves through ``resolve_roster_npc``, whose canonical pass compares
NORMALIZED names — so with "the courier" and "The Courier" both on the roster it
returns whichever is first in roster order, and ``apply_damage`` silently damages
the wrong NPC. Test 8 is the one with teeth: it drives the real tool and watches the
wrong creature bleed.

Tests 9-11 pin ``promote_actor``'s contract, which is currently unpinned in three
places — most importantly that a no-op promotion must NOT emit a
``green_room.actor_promoted`` span. A lie detector that fires on a promotion that
did not happen is not a lie detector.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import NpcMention
from sidequest.dungeon.tactical import TokenAnchor
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.origin import Origin, OriginKind
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.server.narration_apply import _apply_npc_mentions
from sidequest.server.websocket_handlers.map_emit import _place_tokens_on_anchors

_ACTOR_PROMOTED_SPAN = "green_room.actor_promoted"

# The coal: a `generics:` bestiary row seated as the Other. A stat donor with a
# placeholder name, not a person. This is the SEAT ID — the engine's only handle.
_COAL = "the Scrapborn"
# The diamond: the narrator's prose name for that same person. Display only.
_PROSE = "Ihnsch of the Rusted Works"
_HP = 8


@pytest.fixture
def local_otel() -> Iterator[InMemorySpanExporter]:
    """Self-contained in-memory exporter (mirrors the sibling 166-10 suite so this
    file doesn't depend on conftest fixture ordering)."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Fixtures — the 166-10 scene, shared with the sibling suite.
# ---------------------------------------------------------------------------


def _core(name: str, *, hp: int = _HP) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A statted adversary.",
        personality="Relentless.",
        inventory=Inventory(),
        hp=HpPool(current=hp, max=hp, base_max=hp),
        armor_class=12,
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="seaboard_of_saints",
        turn_manager=TurnManager(interaction=1),
    )


def _seat_lone_coal(snap: GameSnapshot) -> Npc:
    """The 166-10 scene: one live Other, GENERIC origin, EMPTY alias ledger — an
    unnamed placeholder waiting for the narrator to name it."""
    coal = Npc(
        core=_core(_COAL),
        origin=Origin(kind=OriginKind.GENERIC, creature_id="scrapborn_raider"),
    )
    snap.npcs.append(coal)
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="resolve", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Magpie", role="scavenger", side="player"),
            EncounterActor(name=_COAL, role="foe", side="opponent"),
        ],
        resolved=False,
    )
    return coal


def _name_the_other(snap: GameSnapshot, prose_name: str = _PROSE) -> None:
    """Drive the REAL narrator-mention path with a hostile prose name — the
    attach-before-mint seated-Other leg (ADR-156 §6). This is the production code
    that performs the promotion; nothing here reaches past it into a helper."""
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name=prose_name, role="hostile")],
        turn_num=6,
    )


def _fate_opponents(snap: GameSnapshot) -> list[Any]:
    """The opponent-side participants of the REAL Fate wire payload — the exact
    objects ``FateConflictSurface`` renders its roster, opponent track, win-meter
    label and attack-target dropdown from."""
    payload = build_fate_state_payload(snap)
    assert payload.conflict is not None, "precondition: the fixture must project a live conflict"
    return [p for p in payload.conflict.participants if p.side == "opponent"]


def _creature_tokens(snap: GameSnapshot) -> list[Any]:
    """The creature tokens of the REAL tactical-grid projection — what the battle
    map draws. ``_place_tokens_on_anchors`` is the production token builder;
    ``_maybe_emit_tactical_grid`` calls it with exactly this shape."""
    tokens = _place_tokens_on_anchors(
        snapshot=snap,
        room_id="salt_camp",
        anchors=[
            TokenAnchor(cell=(1, 1), role="entrance"),
            TokenAnchor(cell=(4, 4), role="creature"),
        ],
    )
    return [t for t in tokens if t.token_id.startswith("creature:")]


# ---------------------------------------------------------------------------
# Tests 1-3 — THE FATE CONFLICT PANEL. Live on 4 of 11 packs. (RED)
# ---------------------------------------------------------------------------


def test_the_fate_conflict_panel_shows_the_stage_name() -> None:
    """The Fate analog of the confrontation panel must carry the stage name.

    ``_project_conflict_participant`` builds ``FateConflictParticipant(name=actor.name,
    ...)`` and never reads ``actor.display_name``, so on pulp_noir /
    spaghetti_western / tea_and_murder / wry_whimsy the promotion fires, the span
    says it fired, and the player still reads "the Scrapborn" in the participants
    roster, the opponent-track heading, the win-meter's screen-reader label and the
    attack-target dropdown — under narration calling it "Ihnsch of the Rusted Works".

    Four of eleven live packs. The server does the work and the projection discards
    it.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    shown = [getattr(p, "display_name", None) for p in _fate_opponents(snap)]
    assert shown == [_PROSE], (
        f"the Fate conflict projection must carry the stage name to the player — it "
        f"is the confrontation panel for pulp_noir, spaghetti_western, tea_and_murder "
        f"and wry_whimsy, and today it drops display_name on the floor "
        f"(fate_projection._project_conflict_participant). display_names={shown!r}"
    )


def test_the_fate_attack_target_still_resolves_by_the_seat_id() -> None:
    """The other half of the same rule, and the one a naive rename fails.

    ``FateConflictParticipant.name`` is not decoration: ``FateConflictSurface`` puts
    it in the attack-target ``<option value=...>``, the client sends it back as
    ``FATE_THROW.target``, and the server's ``_resolve_attack`` resolves the victim
    by it. Repoint it to the prose name to "fix" the display and the player's attack
    targets a name the engine cannot resolve — round 1's failure, relocated to Fate.

    The label is a NEW field. The id does not move.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    ids = [p.name for p in _fate_opponents(snap)]
    assert ids == [_COAL], (
        f"the Fate participant's `name` is the value the attack-target dropdown sends "
        f"back and the server resolves the victim by — it must stay the canonical seat "
        f"id {_COAL!r}. The stage name belongs on a separate display field. ids={ids!r}"
    )


def test_an_unnamed_fate_other_has_no_stage_name() -> None:
    """The regression guard for every Fate conflict that has NOT been promoted —
    overwhelmingly the common case. An Other nobody has named yet must project no
    stage name at all, so the surface falls back to the seat id exactly as it always
    has. A projection that invents a label here (e.g. defaulting ``display_name`` to
    ``name``) would make the fallback untestable and the span meaningless."""
    snap = _snapshot()
    _seat_lone_coal(snap)

    # No narrator mention: the world has not named this enemy.
    shown = [getattr(p, "display_name", None) for p in _fate_opponents(snap)]
    assert shown == [None], (
        f"an un-promoted Other must carry NO stage name — the surface renders "
        f"`display_name ?? name` and must land on the seat id. display_names={shown!r}"
    )


# ---------------------------------------------------------------------------
# Tests 4-6 — THE TACTICAL BATTLE MAP. The same bug, a third time. (RED)
# ---------------------------------------------------------------------------


def test_the_battle_map_token_shows_the_stage_name() -> None:
    """The map token is the third projection that reads ``EncounterActor`` — and the
    most galling one, because ``TokenPayload`` **already has the id/label split this
    whole story is about**: ``token_id`` is the id, ``label`` is the label. ``map_emit``
    feeds the seat id into ``label`` anyway (``label=actor.name``), so the promoted
    Other's token on the battle map reads "the Scrapborn" while the narration and
    (post-fix) the panel both say "Ihnsch of the Rusted Works".

    The UI needs no change for this one: ``TacticalGridRenderer`` renders ``label``
    and derives its token glyph from ``label[0]``. Send the right label and both are
    correct.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    labels = [t.label for t in _creature_tokens(snap)]
    assert labels == [_PROSE], (
        f"TokenPayload.label is the DISPLAY field (TokenPayload.token_id is the id) — "
        f"it must carry the stage name, so the token on the battle map and the name in "
        f"the prose are the same name. labels={labels!r}"
    )


def test_the_battle_map_token_id_stays_canonical() -> None:
    """And the id half: ``token_id`` is ``creature:{seat}``. It keys token identity
    across map refreshes and is how a click on the map resolves back to an actor.
    It must not follow the label."""
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    ids = [t.token_id for t in _creature_tokens(snap)]
    assert ids == [f"creature:{_COAL}"], (
        f"the token's id must stay keyed on the canonical seat id — only `label` "
        f"carries the stage name. token_ids={ids!r}"
    )


def test_the_named_others_map_token_keeps_its_hp() -> None:
    """``_place_tokens_on_anchors`` enriches each creature token by matching the
    roster with ``n.core.name == actor.name`` — a direct, exact, un-normalized
    comparison. It works only because the seat id IS the canonical name. If a fix
    repoints ``actor.name`` to the prose name to get the label right, this match
    misses, and ``hp``/``ac`` go ``None``: the token silently loses its health bar on
    the battle map. Same silent-omission shape as ``_primary_hp`` in the sibling
    suite — no error, the bar just disappears.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    tokens = _creature_tokens(snap)
    assert len(tokens) == 1, f"precondition: the lone Other must be on the map; tokens={tokens!r}"
    hp = tokens[0].hp
    assert hp is not None and (hp.current, hp.max) == (_HP, _HP), (
        f"naming the Other must not cost it its HP bar on the battle map — the roster "
        f"match in _place_tokens_on_anchors is an exact `core.name ==` compare and goes "
        f"None on a miss. hp={hp!r}"
    )


# ---------------------------------------------------------------------------
# Tests 7-8 — THE FOLD COLLISION. The widening damages the WRONG creature. (RED)
# ---------------------------------------------------------------------------


def _two_couriers(snap: GameSnapshot) -> tuple[Npc, Npc]:
    """Two roster NPCs whose names differ ONLY by case. ``normalize_name`` casefolds,
    so they collide under the widened lookup while remaining distinct entities.

    This is reachable in production: the Green Room's dedup gate keys AUTHORED NPCs
    on ``authored_id``, so it does NOT dedup them by name — two authored NPCs may
    legitimately differ only in case (a lowercase generic "the courier" alongside a
    proper-noun "The Courier"), and every pre-gate legacy save can carry any pair at
    all.
    """
    lowercase = Npc(
        core=_core("the courier", hp=5),
        origin=Origin(kind=OriginKind.AUTHORED, authored_id="courier_generic"),
    )
    proper = Npc(
        core=_core("The Courier", hp=99),
        origin=Origin(kind=OriginKind.AUTHORED, authored_id="courier_named"),
    )
    # Roster ORDER matters: the lowercase one is first, so a normalized whole-roster
    # canonical pass returns IT for either query.
    snap.npcs.extend([lowercase, proper])
    return lowercase, proper


def test_find_creature_core_prefers_an_exact_name_over_a_fold_collision() -> None:
    """Story 166-10 widened ``find_creature_core``'s NPC leg from an exact
    ``npc.core.name == name`` match to ``resolve_roster_npc``, whose canonical pass
    compares ``normalize_name(...)`` on BOTH sides. Two NPCs that differ only by case
    now fold together, and the lookup returns whichever is FIRST IN ROSTER ORDER —
    not the one whose name was actually given.

    The widening is right (the narrator must be able to target "Ihnsch"), but it must
    not cost us exactness where exactness was available. An exact hit is never
    ambiguous; try it first, then widen.
    """
    snap = _snapshot()
    _two_couriers(snap)

    proper = snap.find_creature_core("The Courier")
    assert proper is not None and proper.hp.max == 99, (
        f"an EXACT name must resolve to the creature that bears it — 'The Courier' is "
        f"on the roster verbatim. The normalized canonical pass folds it onto the "
        f"lowercase 'the courier' listed before it and returns the wrong stat block. "
        f"resolved hp={None if proper is None else proper.hp!r}"
    )

    lowercase = snap.find_creature_core("the courier")
    assert lowercase is not None and lowercase.hp.max == 5, (
        f"and the other direction must hold too — exactness is not a tiebreak in one "
        f"direction only. resolved hp={None if lowercase is None else lowercase.hp!r}"
    )


def test_the_alias_leg_still_resolves_after_exactness_is_restored() -> None:
    """The guard on the fix for test 7. Restoring the exact-first leg must NOT undo
    the widening that story 166-10 exists to provide: a promoted Other still has to be
    reachable by the prose name the narrator now uses for it, via the alias ledger.

    Exact first, THEN widen. Not exact instead of widen.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    by_prose = snap.find_creature_core(_PROSE)
    by_seat = snap.find_creature_core(_COAL)
    assert by_prose is not None and by_prose is by_seat, (
        f"the alias leg must survive the exactness fix — the narrator knows this enemy "
        f"as {_PROSE!r} and hands that string to apply_damage. "
        f"by_prose={by_prose!r} by_seat={by_seat!r}"
    )


# ---------------------------------------------------------------------------
# Tests 9-11 — ``promote_actor``'s contract. Unpinned in three places.
# ---------------------------------------------------------------------------


def _seat() -> tuple[StructuredEncounter, EncounterActor]:
    snap = _snapshot()
    _seat_lone_coal(snap)
    enc = snap.encounter
    assert enc is not None
    actor = enc.find_actor(_COAL)
    assert actor is not None
    return enc, actor


def _promoted_spans(otel: InMemorySpanExporter) -> list[dict[str, Any]]:
    return [
        dict(s.attributes or {})
        for s in otel.get_finished_spans()
        if s.name == _ACTOR_PROMOTED_SPAN
    ]


def test_promote_actor_refuses_a_blank_stage_name(local_otel: InMemorySpanExporter) -> None:
    """A whitespace-only name is not a name. It must be refused, leave the seat
    unlabelled, and — critically — emit NO promotion span. The GM panel treats
    ``green_room.actor_promoted`` as proof the promotion reached the player's screen;
    a span for a promotion that did not happen makes the lie detector lie."""
    enc, actor = _seat()

    assert enc.promote_actor(actor, "   ") is False, (
        "a blank stage name must be refused, not applied"
    )
    assert actor.display_name is None, (
        f"the seat must be left unlabelled; display_name={actor.display_name!r}"
    )
    assert _promoted_spans(local_otel) == [], (
        f"a refused promotion must emit NO green_room.actor_promoted span — the GM "
        f"panel reads that span as proof the player's panel changed. "
        f"spans={_promoted_spans(local_otel)!r}"
    )


def test_promote_actor_refuses_a_stage_name_equal_to_the_seat_id(
    local_otel: InMemorySpanExporter,
) -> None:
    """Labelling a seat with the id it already displays is a no-op: the player sees
    exactly what they saw before. It must return False and emit no span.

    Today the guard only compares against ``display_name`` (still ``None`` here), so
    this sets a redundant label AND fires a promotion span — the GM panel would report
    a coal→diamond promotion for an enemy whose name never changed.
    """
    enc, actor = _seat()

    assert enc.promote_actor(actor, _COAL) is False, (
        f"a stage name identical to the seat id ({_COAL!r}) changes nothing the player "
        f"can see and must be refused as the no-op it is"
    )
    assert _promoted_spans(local_otel) == [], (
        f"and it must not claim a promotion in OTEL; spans={_promoted_spans(local_otel)!r}"
    )


def test_promote_actor_will_not_relabel_a_seat_that_already_has_a_stage_name(
    local_otel: InMemorySpanExporter,
) -> None:
    """Promotion is coal→diamond: a ONE-WAY door (SOUL *Diamonds and Coal*). Once the
    world has named this enemy, that is its name.

    A second, different stage name must be refused — silently overwriting it would
    make the panel's name CHURN mid-fight ("Ihnsch" on turn 6, something else on turn
    9) while the prose and the alias ledger keep the original. That is the exact
    player-visible name split this story exists to close, re-introduced from the other
    direction. The narrator path already cannot do this (``_attach_before_mint`` gates
    on an empty alias ledger), so the contract is currently held by accident, by a
    caller. Pin it at the seam that actually owns it.
    """
    enc, actor = _seat()
    assert enc.promote_actor(actor, _PROSE) is True, "precondition: the first naming lands"

    assert enc.promote_actor(actor, "Someone Else Entirely") is False, (
        "an already-promoted seat must not be silently relabelled — the panel's name "
        "would churn under the player mid-fight"
    )
    assert actor.display_name == _PROSE, (
        f"the FIRST name the world gave it stands; display_name={actor.display_name!r}"
    )
    assert len(_promoted_spans(local_otel)) == 1, (
        f"and exactly one promotion may be reported to the GM panel; "
        f"spans={_promoted_spans(local_otel)!r}"
    )
