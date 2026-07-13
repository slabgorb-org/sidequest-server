"""RED (rework) — Story 166-10 (ADR-156 §6): the coal→diamond PANEL promotion.

WHAT THIS SUITE GOT WRONG THE FIRST TIME
----------------------------------------
Round 1 of this suite pinned the promoted name on ``actors[].name`` — "the panel
renders that field, so write the prose name into it." Dev implemented exactly
that, all 8 tests went green, and the review found that the enemy had become
**impossible to hit**.

``EncounterActor.name`` is not a label. It is the key the mechanical engine
resolves the opponent by. ``GameSnapshot.find_creature_core`` (``session.py``)
is an EXACT match against ``Npc.core.name``, and it is the ``edge_resolver``
behind ``apply_damage``, ``wn_tools``, ``apply_status``, ``query_encounter`` and
``dice.py``'s ``apply_beat``. The promotion deliberately leaves ``core.name`` as
the coal placeholder (renaming it would re-key ``identity_key`` and fork the
identity — see ``test_promotion_does_not_churn_the_identity``). So the moment
``actor.name`` is repointed to the prose name, the two disagree forever and
every one of those seams returns ``not_found`` for the enemy the player can see.

``encounter_lifecycle.py`` already says so, in a comment written to close an
earlier review's finding on this same seam: *"a seat left under the prose alias
is an unreachable opponent."* The UI says it too
(``ConfrontationOverlay.tsx``): *"that field is a load-bearing entity id ... we
humanize for DISPLAY only and never rewrite the id."*

THE DESIGN THIS SUITE NOW PINS
------------------------------
Two names on two surfaces, not one field doing double duty — which is what
ADR-156 §6 says in the first place ("the combat panel may show 'Molgrath the
Eyeless' **while the engine keys on** ``creature_id=Thief``"):

  * ``EncounterActor.display_name: str | None`` — NEW. The prose name. Display
    only. ``None`` until the world names the coal.
  * ``EncounterActor.name`` — UNCHANGED, canonical, still equal to
    ``Npc.core.name``. Every mechanical seam keeps working untouched.
  * ``find_creature_core`` resolves the roster leg through the alias ledger
    (``resolve_roster_npc``) so the narrator, who now knows the enemy as
    "Ihnsch", can still target it by that name.
  * The UI renders ``display_name ?? name``.

Because nothing is renamed, there is nothing to dangle: no reference sweep, no
initiative/sealed-commit/tag repointing, no two-actors-share-a-name collision.
That entire class of bug disappears rather than being managed.
``test_promotion_leaves_the_seat_id_and_its_references_alone`` pins it.

WHY EACH RED TEST HAS TEETH
---------------------------
  * 1 — the story. The panel must show the prose name (on ``display_name``).
  * 2 — the guard that round 1 lacked: ``actors[].name`` must STAY canonical.
    This is the assertion that fails a rename, and it is the whole review.
  * 3 — the enemy must still be HITTABLE: ``find_creature_core`` must resolve
    the seat. This is the question the first suite never asked.
  * 4 — the narrator must be able to target it by the name it now uses in prose.
  * 5 — the HP bar must survive. ``build_confrontation_payload``'s ``_primary_hp``
    calls ``core_resolver(a.name)``; on an unresolvable name it silently OMITS
    ``opponent_hp`` and the bar vanishes — player-visible, and exactly the
    mechanical legibility the project owes its mechanics-first players.
  * 6 — nothing stamped before the promotion moves, because nothing is renamed.
  * 7/8 — OTEL, both halves: the identity learned the name AND it reached the
    surface.
  * 9-12 — no hijack: authored Others, ambiguous scenes and bystanders are left
    alone. Promotion is for coal.
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
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterTag,
    StructuredEncounter,
    WnSealedCommit,
)
from sidequest.game.origin import (
    Origin,
    OriginKind,
    derive_origin,
    identity_key,
    resolve_roster_npc,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef
from sidequest.protocol.models import InitiativeEntry
from sidequest.server.dispatch.confrontation import build_confrontation_payload
from sidequest.server.narration_apply import _apply_npc_mentions

_ALIAS_ATTACHED_SPAN = "green_room.alias_attached"
_ACTOR_PROMOTED_SPAN = "green_room.actor_promoted"

# The coal: a `generics:` bestiary row seated as the Other. A stat donor with a
# placeholder name, not a person.
_COAL = "the Scrapborn"
# The diamond: the narrator's prose name for that same person.
_PROSE = "Ihnsch of the Rusted Works"
_HP = 8


@pytest.fixture
def local_otel() -> Iterator[InMemorySpanExporter]:
    """Self-contained in-memory exporter (mirrors the attach-before-mint suite
    so this file doesn't depend on conftest fixture ordering)."""
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
# Fixtures — a seated coal Other, plus the real confrontation-panel projection.
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


def _seat_opponents(snap: GameSnapshot, *npcs: Npc) -> StructuredEncounter:
    """Seat every supplied roster Npc as a live opponent-side actor — the shape
    the target-first seater (ADR-156 §3.3) leaves behind before the narrator's
    next mention pass runs. ``win_condition="hp_depletion"`` because that is the
    live binding for every WN pack (ADR-142/143) and it is what makes the panel
    project ``opponent_hp`` at all."""
    snap.npcs.extend(npcs)
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="resolve", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Magpie", role="scavenger", side="player"),
            *[EncounterActor(name=n.core.name, role="foe", side="opponent") for n in npcs],
        ],
        resolved=False,
    )
    return snap.encounter


def _seat_lone_coal(snap: GameSnapshot) -> Npc:
    """The 166-10 scene: one live Other, GENERIC origin, EMPTY alias ledger —
    an unnamed placeholder waiting for the narrator to name it."""
    coal = Npc(
        core=_core(_COAL),
        origin=Origin(kind=OriginKind.GENERIC, creature_id="scrapborn_raider"),
    )
    _seat_opponents(snap, coal)
    return coal


def _cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="Salt Camp Brawl",
        category="combat",
        player_metric=MetricDef(name="resolve", starting=0, threshold=10),
        opponent_metric=MetricDef(name="menace", starting=0, threshold=10),
        beats=[BeatDef(id="swing", label="Swing", kind="push", base=2, stat_check="STR")],
    )


def _panel(snap: GameSnapshot) -> dict[str, Any]:
    """The REAL production panel projection — the dict the UI overlay renders.

    ``core_resolver`` is threaded exactly as production threads it
    (``snapshot.find_creature_core``), because that resolver is what decides
    whether the opponent's HP reaches the player at all."""
    assert snap.encounter is not None
    return build_confrontation_payload(
        encounter=snap.encounter,
        cdef=_cdef(),
        genre_slug=snap.genre_slug,
        core_resolver=snap.find_creature_core,
    )


def _opponents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in payload["actors"] if a["side"] == "opponent"]


def _name_the_other(snap: GameSnapshot, prose_name: str = _PROSE) -> None:
    """Drive the REAL narrator-mention path with a hostile prose name — the
    attach-before-mint seated-Other leg (ADR-156 §6)."""
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name=prose_name, role="hostile")],
        turn_num=6,
    )


# ---------------------------------------------------------------------------
# Test 1 — THE STORY. The panel shows the name the narration used. (RED)
# ---------------------------------------------------------------------------


def test_panel_shows_the_prose_name_after_the_coal_other_is_named() -> None:
    """The narrator names the lone seated coal Other. The panel must carry that
    name to the player — on ``display_name``, the field that exists to be shown.

    Doubles as this suite's WIRING test: it drives the real
    ``_apply_npc_mentions`` and the real ``build_confrontation_payload``.
    """
    snap = _snapshot()
    coal = _seat_lone_coal(snap)

    _name_the_other(snap)

    # The attach half (ADR-156 §6, already implemented) must have fired.
    assert coal.aliases == [_PROSE], (
        f"precondition: the prose name must attach to the seated Other's alias "
        f"ledger; aliases={coal.aliases!r}"
    )

    shown = [a.get("display_name") for a in _opponents(_panel(snap))]
    assert shown == [_PROSE], (
        f"the confrontation panel must carry the name the narration used "
        f"({_PROSE!r}) to the player on ``display_name`` — one enemy, ONE name "
        f"in front of the table. panel display_names={shown!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — THE GUARD ROUND 1 LACKED. The seat id must stay canonical. (RED)
# ---------------------------------------------------------------------------


def test_the_seat_id_the_engine_targets_stays_canonical() -> None:
    """``actors[].name`` is a load-bearing ENTITY ID, not a label — the whole
    finding that rejected round 1.

    ``find_creature_core`` exact-matches it against ``Npc.core.name``, and
    ``core.name`` stays coal (test ``..._does_not_churn_the_identity`` forbids
    renaming it — a GENERIC origin keys ``identity_key`` on the normalized
    display name). So repointing ``actor.name`` to the prose name severs the
    engine's only handle on the opponent, permanently.

    ``encounter_lifecycle`` states the invariant outright: *"a seat left under
    the prose alias is an unreachable opponent."* The UI states it too: the name
    is *"a load-bearing entity id ... we humanize for DISPLAY only and never
    rewrite the id."*

    This assertion FAILS a rename. It is the one that had to exist.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    ids = [a["name"] for a in _opponents(_panel(snap))]
    assert ids == [_COAL], (
        f"the seated Other's entity id must remain the canonical {_COAL!r} — the "
        f"prose name belongs on display_name, NOT on the id every mechanical seam "
        f"resolves the opponent by. panel ids={ids!r}"
    )
    assert snap.encounter is not None
    seat = snap.encounter.find_actor(_COAL)
    assert seat is not None and seat.name == _COAL, (
        f"the seat itself must not be renamed in place; find_actor({_COAL!r}) -> {seat!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — THE QUESTION ROUND 1 NEVER ASKED. Can you still hit it? (RED)
# ---------------------------------------------------------------------------


def test_the_named_other_can_still_be_hit() -> None:
    """After the world names the enemy, the enemy must still be attackable.

    ``GameSnapshot.find_creature_core`` is the ``edge_resolver`` for
    ``apply_beat``/``resolve_opposed_check`` and the target resolver for
    ``apply_damage`` (``core is None`` -> ``ToolResult.not_found``), ``wn_tools``
    (same), ``apply_status``, ``commit_effort``, ``query_encounter`` and
    ``use_mutation``. ``dice.py`` — the PLAYER's own throw — resolves its target
    through it and hands it to ``apply_beat``.

    If it cannot resolve the seat, the player throws the dice, the dice land,
    and nothing happens: no error, no red text, just a narrator improvising a
    miss over an engine that did nothing. That is the exact "convincing narration
    with zero mechanical backing" the project's OTEL doctrine exists to catch.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    assert snap.encounter is not None
    seat = next(a for a in snap.encounter.actors if a.side == "opponent")
    core = snap.find_creature_core(seat.name)
    assert core is not None, (
        f"the seated Other must remain resolvable by its seat id ({seat.name!r}) — "
        f"apply_damage / wn_tools / apply_status / query_encounter and dice.py's "
        f"edge_resolver ALL go through find_creature_core, and a miss here means the "
        f"enemy on the panel cannot be damaged"
    )
    assert core.hp.current == _HP and core.hp.max == _HP, (
        f"and it must resolve to the RIGHT creature — the coal's live stat block; hp={core.hp!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — the narrator now calls it "Ihnsch". That must resolve too. (RED)
# ---------------------------------------------------------------------------


def test_the_narrator_can_target_the_other_by_its_prose_name() -> None:
    """The promotion's whole point is that the world now calls this enemy
    "Ihnsch of the Rusted Works" — so the narrator's next turn will too, and it
    will hand that string to ``apply_damage(target=...)``.

    ``find_creature_core`` must therefore resolve the prose name as well, via
    the alias ledger the attach half already populates (``resolve_roster_npc``:
    canonical -> alias -> invented_from). Both names, one creature. Today the
    roster leg is a bare ``npc.core.name == name`` exact match and this returns
    ``None``.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    by_prose = snap.find_creature_core(_PROSE)
    assert by_prose is not None, (
        f"the narrator knows this enemy as {_PROSE!r} and will target it by that "
        f"name; find_creature_core must resolve it through the alias ledger"
    )
    by_coal = snap.find_creature_core(_COAL)
    assert by_coal is by_prose, (
        f"both names must resolve to the SAME CreatureCore — one enemy, one stat "
        f"block, two names; coal={by_coal!r} prose={by_prose!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — the HP bar must survive the promotion. (RED)
# ---------------------------------------------------------------------------


def test_the_panel_still_shows_the_others_hp_after_it_is_named() -> None:
    """``build_confrontation_payload``'s ``_primary_hp`` resolves each side's
    primary combatant with ``core_resolver(a.name)`` and, on a miss, **silently
    omits** the ``opponent_hp`` key — the HP bar just vanishes from the overlay.
    No error, no span, nothing.

    That HP readout is the player-facing mechanical legibility this project owes
    its mechanics-first players. A promotion that trades the enemy's name for the
    enemy's health bar is not a fix.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    payload_before = _panel(snap)
    assert payload_before.get("opponent_hp") == {"current": _HP, "max": _HP}, (
        f"precondition: the panel shows the Other's HP before it is named; "
        f"opponent_hp={payload_before.get('opponent_hp')!r}"
    )

    _name_the_other(snap)

    payload_after = _panel(snap)
    assert payload_after.get("opponent_hp") == {"current": _HP, "max": _HP}, (
        f"naming the Other must not cost the player its HP bar — _primary_hp "
        f"silently drops the key when core_resolver misses the seat id; "
        f"opponent_hp={payload_after.get('opponent_hp')!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 — nothing is renamed, so nothing dangles. (RED — pins the invariant)
# ---------------------------------------------------------------------------


def test_promotion_leaves_the_seat_id_and_its_references_alone() -> None:
    """The structural payoff of promoting on ``display_name`` instead of
    renaming: every reference stamped with the seat id in an earlier turn is
    untouched, because the seat id never moves.

    These are the structures that hold an actor name by EXACT string and outlive
    a turn — the initiative token (the WN round walk's turn order), the WN
    sealed-commit ledger, and scene tags. Under a rename each one has to be
    hunted down and repointed, and any one missed dangles silently (the impact
    badge drops, the round walk skips a seat). Under a display-only promotion
    there is nothing to hunt: this test asserts they are all exactly as they
    were.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)
    enc = snap.encounter
    assert enc is not None
    enc.initiative = [InitiativeEntry(token_id=_COAL, value=11)]
    enc.wn_commits = [WnSealedCommit(actor=_COAL, beat_id="swing", outcome="Success", target=_COAL)]
    enc.tags = [
        EncounterTag(text="Off-balance", created_by=_COAL, target=_COAL, leverage=1, created_turn=5)
    ]

    _name_the_other(snap)

    assert enc.initiative[0].token_id == _COAL, (
        f"the initiative token must still name the seat id — the WN round walk "
        f"keys on it; token_id={enc.initiative[0].token_id!r}"
    )
    assert enc.wn_commits[0].actor == _COAL and enc.wn_commits[0].target == _COAL, (
        f"a sealed commit stamped before the promotion must still resolve; "
        f"commit={enc.wn_commits[0]!r}"
    )
    assert enc.tags[0].created_by == _COAL and enc.tags[0].target == _COAL, (
        f"a scene tag stamped before the promotion must still resolve; tag={enc.tags[0]!r}"
    )
    assert enc.find_actor(_COAL) is not None, (
        "and the seat is still findable by the id all of them carry"
    )


# ---------------------------------------------------------------------------
# Tests 7-8 — OTEL. Both halves of the promotion must be visible.
# ---------------------------------------------------------------------------


def test_promotion_emits_the_alias_attached_span(local_otel: InMemorySpanExporter) -> None:
    """Half one: the IDENTITY learned the name (``green_room.attach_alias``)."""
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    attached = [
        dict(s.attributes or {})
        for s in local_otel.get_finished_spans()
        if s.name == _ALIAS_ATTACHED_SPAN
    ]
    assert any(a.get("alias") == _PROSE for a in attached), (
        f"green_room.alias_attached must fire for the promotion — the GM panel is "
        f"the lie detector; spans={attached!r}"
    )


def test_promotion_emits_the_actor_promoted_span(local_otel: InMemorySpanExporter) -> None:
    """Half two: the name reached the SURFACE (``promote_actor``). (RED)

    ``alias_attached`` alone cannot distinguish "the panel now shows Ihnsch" from
    "the ledger learned Ihnsch and the player still stares at 'the Scrapborn'" —
    which is precisely the bug this story exists to close. The GM panel needs a
    span for the display half or it cannot tell the fix fired.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    promoted = [
        dict(s.attributes or {})
        for s in local_otel.get_finished_spans()
        if s.name == _ACTOR_PROMOTED_SPAN
    ]
    assert any(
        p.get("display_name") == _PROSE
        and p.get("seat_id") == _COAL
        and p.get("side") == "opponent"
        for p in promoted
    ), (
        f"green_room.actor_promoted must fire carrying the seat id it promoted and "
        f"the display name it gave it — proof the promotion reached the surface the "
        f"player is looking at, not just the ledger; spans={promoted!r}"
    )


# ---------------------------------------------------------------------------
# Tests 9-12 — invariants + no hijack. Promotion is for COAL, not everything.
# ---------------------------------------------------------------------------


def test_promotion_does_not_churn_the_identity() -> None:
    """ADR-156 §6: the prose name is a *stage name*, "recorded against the
    identity it describes — never a new identity".

    The ``identity_key`` assertion is load-bearing: for a GENERIC origin the key
    IS the normalized display name (a ``generics:`` row is a stat donor, not an
    identity), so "promoting" by rewriting ``Npc.core.name`` silently re-keys the
    identity and forks the very ledger §6 exists to prevent. THIS is why the
    canonical name must stay coal — and therefore why ``actor.name`` (which must
    equal it) cannot carry the prose name either. The two constraints are the
    same constraint.
    """
    snap = _snapshot()
    coal = _seat_lone_coal(snap)
    key_before = identity_key(derive_origin(coal), coal.core.name)
    roster_before, pool_before = len(snap.npcs), len(snap.npc_pool)

    _name_the_other(snap)

    assert len(snap.npcs) == roster_before, (
        f"no NEW identity may be minted; roster={[n.core.name for n in snap.npcs]!r}"
    )
    assert len(snap.npc_pool) == pool_before, (
        f"no phantom twin in the pool; pool={[m.name for m in snap.npc_pool]!r}"
    )
    assert resolve_roster_npc(snap.npcs, _PROSE) is coal, (
        "the prose name must resolve to the SAME Npc object, not a copy"
    )
    assert identity_key(derive_origin(coal), coal.core.name) == key_before, (
        f"identity_key must be STABLE across the promotion; was {key_before!r}"
    )
    assert coal.core.name == _COAL, (
        f"the CANONICAL name stays coal — the promotion is display-layer; "
        f"core.name={coal.core.name!r}"
    )
    assert coal.core.hp.current == _HP and coal.core.hp.max == _HP, (
        f"mechanics are untouched by a display rename; hp={coal.core.hp!r}"
    )


def test_authored_other_is_not_renamed_by_a_hostile_mention() -> None:
    """An AUTHORED Other already has a proper name — it is a diamond, not coal.
    A hostile prose mention naming someone else must never repaint the panel's
    named villain, on ``display_name`` or anywhere else."""
    snap = _snapshot()
    villain = Npc(
        core=_core("Warden Cull"),
        origin=Origin(kind=OriginKind.AUTHORED, authored_id="warden_cull"),
    )
    _seat_opponents(snap, villain)

    _name_the_other(snap, "some other bruiser")

    opponents = _opponents(_panel(snap))
    assert [a["name"] for a in opponents] == ["Warden Cull"], "the authored id must not move"
    assert [a.get("display_name") for a in opponents] == [None], (
        f"an authored, already-named Other must NOT be given a stage name by a "
        f"passing hostile mention; display_names={[a.get('display_name') for a in opponents]!r}"
    )


def test_two_live_opponents_are_ambiguous_and_neither_is_renamed() -> None:
    """Two live Others: the prose name could belong to either. Never guess (No
    Silent Fallbacks) — neither seat may be promoted."""
    snap = _snapshot()
    first = Npc(core=_core(_COAL), origin=Origin(kind=OriginKind.GENERIC, creature_id="s_raider"))
    second = Npc(
        core=_core("the courier"), origin=Origin(kind=OriginKind.GENERIC, creature_id="s_raider")
    )
    _seat_opponents(snap, first, second)

    _name_the_other(snap)

    opponents = _opponents(_panel(snap))
    assert [a["name"] for a in opponents] == [_COAL, "the courier"]
    assert [a.get("display_name") for a in opponents] == [None, None], (
        f"with two live Others the prose name is ambiguous — neither seat may be "
        f"promoted; display_names={[a.get('display_name') for a in opponents]!r}"
    )


def test_bystander_mention_does_not_rename_the_seated_other() -> None:
    """A non-hostile mention is a bystander walking through the scene, not the
    enemy's true name. It must not glue onto the Other."""
    snap = _snapshot()
    _seat_lone_coal(snap)

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Tallow the fishmonger", role="merchant")],
        turn_num=6,
    )

    opponents = _opponents(_panel(snap))
    assert [a["name"] for a in opponents] == [_COAL]
    assert [a.get("display_name") for a in opponents] == [None], (
        f"a bystander mention must not promote the seated Other; "
        f"display_names={[a.get('display_name') for a in opponents]!r}"
    )
