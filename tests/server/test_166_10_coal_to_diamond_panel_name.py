"""RED — Story 166-10 (ADR-156 §6): the coal→diamond PANEL promotion.

THE PLAYER-VISIBLE SPLIT
------------------------
ADR-156 §6's attach-before-mint already works: when the narrator's prose names
the lone seated Other, ``narration_apply._attach_before_mint`` records that
prose name as an alias on the Other's identity (``green_room.attach_alias`` →
``Npc.aliases``) instead of minting a phantom twin. ``test_green_room_attach_
before_mint.py`` pins that half and it is GREEN.

The half that is NOT wired is the DISPLAY. ``attach_alias`` appends to
``Npc.aliases`` and touches nothing else. The confrontation panel does not read
the alias ledger — ``build_confrontation_payload``'s ``_actor_with_portrait``
serializes ``EncounterActor.model_dump()`` verbatim, and ``EncounterActor.name``
was baked with the coal placeholder once, at seat time
(``encounter_lifecycle.py``). So the table reads:

    NARRATION:  "Ihnsch of the Rusted Works spits and raises the bar."
    PANEL:      [ the Scrapborn ]  HP 8/8

One enemy, two names, both on screen. That is the bug 166-10 closes: a flavor
name landing on a bare generic is an ADR-128 promotion signal (SOUL *Diamonds
and Coal* — the world cared enough to name it), and the promotion must reach the
surface the player is actually looking at.

WHAT THIS SUITE PINS (and why each assertion has teeth)
-------------------------------------------------------
The naive fix — ``actor.name = alias`` — passes tests 1 and 3 and BREAKS tests 2
and 4. That is deliberate. ``EncounterActor.name`` is a load-bearing entity id,
not a label: ``StructuredEncounter.find_actor`` is an exact string match
(``encounter.py:446``), and the UI states outright that ``actor.name`` is "a
load-bearing entity id (tag targets / last_beat_impacts keys reference it)"
(``ConfrontationOverlay.tsx:649-656``). So a promotion has to satisfy BOTH ends:

  * test 1 — the panel payload the UI renders shows the prose name.
  * test 2 — the IDENTITY does not churn. ``identity_key`` is asserted stable,
    which forbids rewriting ``Npc.core.name``: for a GENERIC origin the key IS
    the normalized display name (``origin.py:101-118``), so renaming the
    canonical would mint a NEW identity — precisely what §6 forbids ("recorded
    against the identity it describes — never a new identity"). HP, creature_id,
    roster length and pool length are pinned with it.
  * test 3 — the promoted name resolves: the engine can find the seated actor by
    what the panel now shows. Without this, the UI targets a name the engine
    cannot look up.
  * test 4 — the PLACEHOLDER still resolves too. References stamped with the old
    name in earlier turns (tag targets, ``last_beat_impacts`` keys, pending
    commits) must not dangle. "One enemy, one identity, TWO names" (§6) is a
    two-way contract, and this is the assertion a naive in-place rename fails.
  * tests 5-7 — no hijack. A proper-named (authored) Other, an ambiguous
    two-opponent scene, and a non-hostile bystander mention must each leave the
    panel name alone. Promotion is for coal, not for everything.
  * test 8 — OTEL. Per the project's OTEL Observability Principle the promotion
    emits ``green_room.alias_attached``; the GM panel is the lie detector for
    whether the promotion fired or the narrator simply improvised a name.

Tests drive the REAL ``_apply_npc_mentions`` and the REAL
``build_confrontation_payload`` end-to-end across both seams (the attach site and
the projection site) — no source-text greps (CLAUDE.md "No Source-Text Wiring
Tests"), and test 1 doubles as this suite's wiring test.
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
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
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
from sidequest.server.dispatch.confrontation import build_confrontation_payload
from sidequest.server.narration_apply import _apply_npc_mentions

_ALIAS_ATTACHED_SPAN = "green_room.alias_attached"

# The coal: a `generics:` bestiary row seated as the Other. A stat donor with a
# placeholder name, not a person.
_COAL = "the Scrapborn"
# The diamond: the narrator's prose name for that same person.
_PROSE = "Ihnsch of the Rusted Works"


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


def _core(name: str, *, hp: int = 8) -> CreatureCore:
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
    next mention pass runs. The PC is seated alongside so opponent-scoping is
    observable."""
    snap.npcs.extend(npcs)
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
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
    """The REAL production panel projection — the dict the UI overlay renders."""
    assert snap.encounter is not None
    return build_confrontation_payload(
        encounter=snap.encounter,
        cdef=_cdef(),
        genre_slug=snap.genre_slug,
    )


def _opponent_names(payload: dict[str, Any]) -> list[str]:
    """Every opponent-side name the panel puts in front of the player."""
    return [a["name"] for a in payload["actors"] if a["side"] == "opponent"]


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
    """The narrator names the lone seated coal Other "Ihnsch of the Rusted
    Works". The alias attaches (already GREEN) — and the confrontation panel
    must now READ that promotion. Pre-implementation the panel still projects
    the seat-time placeholder, so narration and panel disagree in front of the
    player.

    Asserted on ``actors[].name`` because that is the field the overlay actually
    renders (``ConfrontationOverlay.tsx:1219`` → ``humanizeActorName(opponent.
    name)``). A fix that instead adds a NEW key (e.g. ``display_name``) leaves
    the rendered name unchanged and the split still on screen — it would need a
    matching sidequest-ui change, which this server-scoped story does not carry.

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

    names = _opponent_names(_panel(snap))
    assert names == [_PROSE], (
        f"the confrontation panel must show the name the narration used "
        f"({_PROSE!r}), not the seat-time coal placeholder ({_COAL!r}) — "
        f"one enemy, one identity, ONE name in front of the player. "
        f"panel opponents={names!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — the promotion is DISPLAY-layer. The identity must not churn.
# ---------------------------------------------------------------------------


def test_promotion_does_not_churn_the_identity() -> None:
    """ADR-156 §6: the prose name is a *stage name*, "recorded against the
    identity it describes — never a new identity". The engine keys on the
    creature; only the marquee changes.

    The ``identity_key`` assertion is the load-bearing one: for a GENERIC origin
    the key IS the normalized display name (``origin.py:101-118``, the stat-donor
    exception), so a fix that "promotes" by rewriting ``Npc.core.name`` silently
    re-keys the identity and forks the very ledger §6 exists to prevent. The
    canonical name stays coal; the alias carries the diamond.
    """
    snap = _snapshot()
    coal = _seat_lone_coal(snap)
    key_before = identity_key(derive_origin(coal), coal.core.name)
    roster_before, pool_before = len(snap.npcs), len(snap.npc_pool)

    _name_the_other(snap)

    assert len(snap.npcs) == roster_before, (
        f"no NEW identity may be minted — the prose name attaches to the seated "
        f"Other; roster={[n.core.name for n in snap.npcs]!r}"
    )
    assert len(snap.npc_pool) == pool_before, (
        f"no phantom twin in the pool; pool={[m.name for m in snap.npc_pool]!r}"
    )
    resolved = resolve_roster_npc(snap.npcs, _PROSE)
    assert resolved is coal, (
        f"the prose name must resolve to the SAME Npc object, not a copy; got {resolved!r}"
    )
    assert identity_key(derive_origin(coal), coal.core.name) == key_before, (
        f"identity_key must be STABLE across the promotion (a GENERIC origin keys "
        f"on the normalized canonical name — rewriting core.name forks the "
        f"identity); was {key_before!r}"
    )
    assert coal.core.name == _COAL, (
        f"the CANONICAL name stays coal — the promotion is display-layer; "
        f"core.name={coal.core.name!r}"
    )
    assert coal.creature_id == "scrapborn_raider" or (
        derive_origin(coal).creature_id == "scrapborn_raider"
    ), "the stat donor (creature_id) must survive the promotion"
    assert coal.core.hp.current == 8 and coal.core.hp.max == 8, (
        f"mechanics are untouched by a rename; hp={coal.core.hp!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — the engine can find the actor by the name the panel shows.
# ---------------------------------------------------------------------------


def test_seated_actor_is_findable_by_the_promoted_name() -> None:
    """The engine must resolve the PROMOTED name. The UI hands ``actor.name``
    back as a tag target, and every targeting seam funnels through
    ``StructuredEncounter.find_actor`` — an EXACT string match. A panel name the
    engine cannot look up is a dead click.

    Asserted against the prose name DIRECTLY, not against whatever the panel
    happens to be showing: deriving the lookup key from the panel makes the test
    tautological (pre-implementation the panel shows the seat name, so the
    lookup trivially succeeds and the test passes while the bug is wide open).
    The second assertion then pins the coupling — the name the panel shows must
    be exactly the name the engine can find.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    assert snap.encounter is not None
    found = snap.encounter.find_actor(_PROSE)
    assert found is not None and found.side == "opponent", (
        f"the engine must find the seated Other by its promoted name ({_PROSE!r}) — "
        f"the UI hands this string back as a tag target; find_actor returned {found!r}"
    )
    shown = _opponent_names(_panel(snap))[0]
    assert shown == found.name, (
        f"the name on the panel ({shown!r}) and the name the engine resolves "
        f"({found.name!r}) must be the same string — that split IS the bug"
    )


# ---------------------------------------------------------------------------
# Test 4 — the placeholder must STILL resolve. Old references cannot dangle.
# ---------------------------------------------------------------------------


def test_seated_actor_is_still_findable_by_the_original_placeholder() -> None:
    """One enemy, one identity, TWO names (§6) — and that is a two-way contract.

    Earlier turns stamped the PLACEHOLDER into structures that outlive the
    rename — ``last_beat_impacts`` keys and tag targets both reference
    ``actor.name`` (``ConfrontationOverlay.tsx:649-656``), and a pending sealed
    commit may carry it. A naive in-place ``actor.name = alias`` rename orphans
    every one of them: the impact badge silently drops, the tag target misses.

    This is the assertion that forces the promotion to be alias-AWARE rather
    than destructive. Both names must land on the same seated actor.
    """
    snap = _snapshot()
    _seat_lone_coal(snap)

    _name_the_other(snap)

    assert snap.encounter is not None
    by_old = snap.encounter.find_actor(_COAL)
    assert by_old is not None and by_old.side == "opponent", (
        f"a reference stamped with the pre-promotion placeholder ({_COAL!r}) must "
        f"still resolve to the seated Other — earlier-turn tag targets and "
        f"last_beat_impacts keys hold that string and must not dangle; "
        f"find_actor returned {by_old!r}"
    )
    by_new = snap.encounter.find_actor(_PROSE)
    assert by_new is by_old, (
        f"both names must land on the SAME seated actor (one identity, two "
        f"names); old={by_old!r} new={by_new!r}"
    )


# ---------------------------------------------------------------------------
# Tests 5-7 — no hijack. Promotion is for COAL, not for everything.
# ---------------------------------------------------------------------------


def test_authored_other_is_not_renamed_by_a_hostile_mention() -> None:
    """An AUTHORED Other already has a proper name — it is a diamond, not coal.
    A hostile prose mention naming someone else must never repaint the panel's
    named villain. (``_attach_before_mint``'s seated-Other leg gates on
    GENERIC/NARRATOR_INVENTED origin with an EMPTY ledger; this pins that the
    PANEL honours the same gate.)"""
    snap = _snapshot()
    villain = Npc(
        core=_core("Warden Cull"),
        origin=Origin(kind=OriginKind.AUTHORED, authored_id="warden_cull"),
    )
    _seat_opponents(snap, villain)

    _name_the_other(snap, "some other bruiser")

    names = _opponent_names(_panel(snap))
    assert names == ["Warden Cull"], (
        f"an authored, already-named Other must NOT be renamed by a passing "
        f"hostile mention; panel opponents={names!r}"
    )


def test_two_live_opponents_are_ambiguous_and_neither_is_renamed() -> None:
    """Two live Others: the prose name could belong to either. Never guess (No
    Silent Fallbacks) — ``_attach_before_mint`` falls through to mint, and the
    panel must keep both seat names untouched."""
    snap = _snapshot()
    first = Npc(core=_core(_COAL), origin=Origin(kind=OriginKind.GENERIC, creature_id="s_raider"))
    second = Npc(
        core=_core("the courier"), origin=Origin(kind=OriginKind.GENERIC, creature_id="s_raider")
    )
    _seat_opponents(snap, first, second)

    _name_the_other(snap)

    names = _opponent_names(_panel(snap))
    assert names == [_COAL, "the courier"], (
        f"with two live Others the prose name is ambiguous — neither seat may be "
        f"renamed; panel opponents={names!r}"
    )


def test_bystander_mention_does_not_rename_the_seated_other() -> None:
    """A non-hostile mention is a bystander walking through the scene, not the
    enemy's true name. It must not glue onto the Other (``_mention_is_hostile``
    gates the attach) — and so must not repaint the panel."""
    snap = _snapshot()
    _seat_lone_coal(snap)

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Tallow the fishmonger", role="merchant")],
        turn_num=6,
    )

    names = _opponent_names(_panel(snap))
    assert names == [_COAL], (
        f"a bystander mention must not rename the seated Other; panel opponents={names!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — OTEL. The GM panel is the lie detector for the promotion.
# ---------------------------------------------------------------------------


def test_promotion_emits_the_alias_attached_span(local_otel: InMemorySpanExporter) -> None:
    """Project doctrine (OTEL Observability Principle): every subsystem decision
    emits a span the GM panel can verify. Without it there is no way to tell a
    real coal→diamond promotion from the narrator simply improvising a name in
    prose while the engine did nothing."""
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
