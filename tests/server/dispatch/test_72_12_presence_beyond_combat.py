"""Story 72-12 — presence-stamp ``last_seen_turn`` / ``last_seen_location`` at the
encounter seams 72-8 did NOT cover: the ``opposed_check`` social-resolution path
and the ``participant_joined`` seating path. Doctrine: "presence means presence" —
any time an NPC is a seated opponent or joins as a participant, its recency is
stamped, regardless of whether the seam is combat-category.

72-8 closed two COMBAT seams only (``_seed_combat_hp_depletion_to_npcs`` and
``_publish_combat_edge_to_npcs``), both gated behind ``cdef.category == "combat"``
in ``instantiate_encounter_from_trigger``. So a non-combat opponent — a social
duellist seated as ``opponent`` via ``opposed_check`` — went un-stamped while it
was demonstrably present and trading dice rolls with the party. 72-6's
last-seen prune then read that present opponent as stale.

This suite drives the two NEW seams through their PRODUCTION call paths (no
direct call to a private stamp helper — that would not prove wiring):

* Seam 1 — ``narration_apply._apply_narration_result_to_snapshot`` →
  ``_resolve_opposed_check_branch`` (the real social-duel resolution handshake).
* Seam 2 — ``encounter_lifecycle.instantiate_encounter_from_trigger`` (the real
  seating path that fires ``participant_joined_span`` per actor).

Mirrors the write discipline 72-8 established: ``last_seen_turn`` always advances
to the encounter turn; ``last_seen_location`` is only written when a location
resolved (No Silent Fallbacks — never clobber with a bogus/empty value). Each
stamp surfaces on an OTEL span so the GM-panel lie-detector can confirm it fired
(AC2 → ``npc.edge_published``; AC4 → ``participant.joined``).

AC6 (no regression on the existing 72-8 combat seams) is guarded by the untouched
``tests/server/dispatch/test_72_8_presence_last_seen_stamp.py`` staying green; it
is not re-implemented here.

These tests rely on the real ``tea_and_murder`` content pack (the ``social_duel``
cdef carries the opposed-check beats + opponent stats); skipped when absent, the
same guard ``test_glenross_social_duel_opposed_check.py`` already uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[3].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)

_PLAYER = "Inspector Pryce"
_OPPONENT = "Sir Iain Ross"
_HALL = "Castle Ross — The Great Hall"
# A DIFFERENT prior location so a "froze" assertion can't accidentally pass by
# matching the hall the player is standing in.
_STALE_LOC = "The Old Library"
_STALE_TURN = 2


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


def _make_opponent_npc(*, location: str | None, turn: int) -> Npc:
    return Npc(
        core=CreatureCore(
            name=_OPPONENT,
            description="The laird of Glenross.",
            personality="Proud, sharp-tongued.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        npc_role_id="hostile",
        last_seen_location=location,
        last_seen_turn=turn,
    )


def _social_duel_encounter() -> StructuredEncounter:
    """A live Duel of Wits: Pryce (player) vs Sir Iain (opponent), 0/7 dials.

    Both carry per_actor_state stats so the opposed-check modifier resolves
    cleanly (cloned from test_glenross_social_duel_opposed_check.py)."""
    return StructuredEncounter(
        encounter_type="social_duel",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="barbs_landed", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="barbs_landed", current=0, starting=0, threshold=7),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name=_PLAYER,
                role="participant",
                side="player",
                per_actor_state={"stats": {"Cunning": 12, "Nerve": 12, "Humour": 12}},
            ),
            EncounterActor(
                name=_OPPONENT,
                role="participant",
                side="opponent",
                per_actor_state={"stats": {"Cunning": 10, "Nerve": 10, "Humour": 10}},
            ),
        ],
    )


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _drive_opposed_duel(snap: GameSnapshot, monkeypatch) -> None:
    """Resolve one social_duel round through the real opposed_check handshake.

    Sir Iain rolls 18 (Success); Pryce rolls 5 (Fail). Tiers are irrelevant to
    presence-stamping — what matters is that Sir Iain is a SEATED opponent this
    turn, so his recency must be stamped regardless of outcome.
    """
    from sidequest.server import narration_apply as _na

    monkeypatch.setattr(_na, "_roll_d20_server_side", lambda: 18)

    result = NarrationTurnResult(
        narration="",  # NB: no prose name-drop — stamp must come from PRESENCE.
        beat_selections=[
            BeatSelection(actor=_OPPONENT, beat_id="riposte", outcome=RollOutcome.Success),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name=_PLAYER,
        pack=_pack(),
        opposed_player_d20=5,
        opposed_player_beat_id="riposte",
        opposed_player_actor=_PLAYER,
        from_explicit_action=True,
        room=room_for(snap),
    )


# ---------------------------------------------------------------------------
# Seam 1 — opposed_check social resolution
# ---------------------------------------------------------------------------


def test_opposed_check_social_stamps_presence_without_prose_mention(monkeypatch) -> None:
    """AC1 — an NPC seated as the opponent in an opposed_check social duel gets
    ``last_seen_turn`` advanced to the encounter turn and ``last_seen_location``
    set to the acting PC's location, even though the narrator never names it in
    prose. Driven through the production resolution handshake (wiring guard)."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[_PLAYER] = _HALL
    snap.encounter = _social_duel_encounter()
    npc = _make_opponent_npc(location=_STALE_LOC, turn=_STALE_TURN)
    snap.npcs.append(npc)

    _drive_opposed_duel(snap, monkeypatch)

    assert npc.last_seen_turn == 5, (
        "an opposed_check social opponent is PRESENT this turn; its last_seen_turn "
        f"must advance to the encounter turn (5), got {npc.last_seen_turn}"
    )
    assert npc.last_seen_location == _HALL, (
        "presence stamp must set last_seen_location to the acting PC's resolved "
        f"location, got {npc.last_seen_location!r}"
    )


def test_opposed_check_presence_stamp_rides_npc_edge_published_span(
    otel_capture, monkeypatch
) -> None:
    """AC2 — the opposed_check presence stamp is surfaced as
    ``last_seen_turn`` / ``last_seen_location`` attributes on a
    ``npc.edge_published`` span (the same GM-panel lie-detector span family 72-8
    uses for the combat seams), not buried in an un-observable mutation."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[_PLAYER] = _HALL
    snap.encounter = _social_duel_encounter()
    snap.npcs.append(_make_opponent_npc(location=_STALE_LOC, turn=_STALE_TURN))

    _drive_opposed_duel(snap, monkeypatch)

    edge_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "npc.edge_published"
    ]
    assert edge_spans, (
        "opposed_check presence stamp never emitted a npc.edge_published span; "
        f"finished spans={[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    # ``npc_edge_published_span`` stores the NPC name under ``npc_name`` (not
    # ``name``) — filter on the real key and fail loud if no span matches the
    # opponent, rather than silently falling back to "any edge span".
    stamped = [
        s for s in edge_spans if (dict(s.attributes or {})).get("npc_name") == _OPPONENT
    ]
    assert stamped, (
        "no npc.edge_published span carried npc_name=={!r}; emitted npc_names={!r}".format(
            _OPPONENT,
            [dict(s.attributes or {}).get("npc_name") for s in edge_spans],
        )
    )
    # Exactly one stamp per turn — a double-emit would let a stale second span
    # hide behind stamped[0].
    assert len(stamped) == 1, (
        f"expected exactly 1 npc.edge_published for {_OPPONENT!r}, got {len(stamped)}"
    )
    attrs = dict(stamped[0].attributes or {})
    assert attrs.get("last_seen_turn") == 5, (
        f"span missing/incorrect last_seen_turn presence stamp; attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_location") == _HALL, (
        f"span missing/incorrect last_seen_location presence stamp; attrs={sorted(attrs)!r}"
    )


def test_opposed_check_no_resolved_location_stamps_turn_not_location(monkeypatch) -> None:
    """AC5 (seam 1) — when the acting PC has no resolved location,
    ``party_location`` returns None: the presence stamp advances
    ``last_seen_turn`` but must NOT overwrite ``last_seen_location`` with a
    bogus/empty value — it stays frozen (No Silent Fallbacks, mirrors 72-8)."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=5),
    )
    # No character_locations entry for the acting PC → party_location is None.
    snap.encounter = _social_duel_encounter()
    npc = _make_opponent_npc(location=_STALE_LOC, turn=_STALE_TURN)
    snap.npcs.append(npc)

    _drive_opposed_duel(snap, monkeypatch)

    assert npc.last_seen_turn == 5, "turn must still advance when location is unresolved"
    assert npc.last_seen_location == _STALE_LOC, (
        "an unresolved location must NOT clobber the prior last_seen_location with "
        f"a bogus value; got {npc.last_seen_location!r}"
    )


# ---------------------------------------------------------------------------
# Seam 2 — participant_joined seating (non-combat: 72-8's combat seams cannot fire)
# ---------------------------------------------------------------------------


def test_participant_joined_stamps_presence_on_seating() -> None:
    """AC3 — an NPC seated as a participant via the location-fallback join gets
    ``last_seen_turn`` / ``last_seen_location`` stamped at seating time.

    Driven through ``instantiate_encounter_from_trigger`` for the NON-combat
    ``social_duel`` (``cdef.category != "combat"``), so the 72-8 combat-edge
    seams are gated OFF — the only possible stamp source is the new
    participant_joined seam. This is the wiring guard for seam 2.

    NB on the location fixture: the location-FALLBACK seating path
    (``_npc_fallback_at_location``) can only seat an NPC whose
    ``last_seen_location`` ALREADY equals the player's location — that is how the
    fallback finds it. So an NPC seated this way is co-located BY CONSTRUCTION
    and its ``last_seen_location`` cannot differ before/after; the discriminating
    proof for THIS path is the TURN advance (``_STALE_TURN`` → 6). The
    location-WRITE proof (prior location ≠ player location) lives in
    ``test_participant_joined_router_named_stamps_location`` (router-named path,
    where co-location is not required) and in the opposed_check AC1 test."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=6),
    )
    snap.character_locations[_PLAYER] = _HALL
    # Co-located at _HALL: required for the location-fallback to seat this NPC
    # (see docstring). The turn is stale (2) so the turn advance is the real proof.
    npc = _make_opponent_npc(location=_HALL, turn=_STALE_TURN)
    snap.npcs.append(npc)

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="social_duel",
        player_name=_PLAYER,
        npcs_present=[],  # location fallback seats Sir Iain as the opposed_check Other.
        genre_slug="tea_and_murder",
    )

    assert npc.last_seen_turn == 6, (
        "an NPC seated as a participant is PRESENT; its last_seen_turn must advance "
        f"to the seating turn (6), got {npc.last_seen_turn}"
    )
    # Co-location INVARIANT (not the write-proof — the location-write proof lives in
    # test_participant_joined_router_named_stamps_location): a location-fallback-seated
    # NPC is necessarily already at the player's location, so this guards only that the
    # stamp does not CLOBBER an already-correct value.
    assert npc.last_seen_location == _HALL, (
        "co-location invariant: location-fallback seating must not clobber the "
        f"already-correct last_seen_location; got {npc.last_seen_location!r}"
    )


def test_participant_joined_stamp_rides_participant_joined_span(otel_capture) -> None:
    """AC4 — the participant_joined presence stamp is surfaced as
    ``last_seen_turn`` / ``last_seen_location`` attributes on the
    ``participant.joined`` span itself, distinct from the side/source attributes
    already present (GM-panel lie-detector).

    Location fixture co-located at _HALL (same fallback-seating constraint as AC3:
    a location-fallback NPC is already at the player's location). The TURN
    (``_STALE_TURN`` → 6) is the discriminating span proof here; the
    location-write proof lives in the router-named test."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=6),
    )
    snap.character_locations[_PLAYER] = _HALL
    snap.npcs.append(_make_opponent_npc(location=_HALL, turn=_STALE_TURN))

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="social_duel",
        player_name=_PLAYER,
        npcs_present=[],
        genre_slug="tea_and_murder",
    )

    joined = [
        s for s in otel_capture.get_finished_spans() if s.name == "participant.joined"
    ]
    assert joined, (
        "no participant.joined span fired; "
        f"finished={[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    iain = [s for s in joined if (dict(s.attributes or {})).get("name") == _OPPONENT]
    assert iain, (
        "no participant.joined span for the seated NPC opponent; "
        f"names={[dict(s.attributes or {}).get('name') for s in joined]!r}"
    )
    attrs = dict(iain[0].attributes or {})
    assert attrs.get("last_seen_turn") == 6, (
        f"participant.joined span missing/incorrect last_seen_turn; attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_location") == _HALL, (
        f"participant.joined span missing/incorrect last_seen_location; attrs={sorted(attrs)!r}"
    )


def test_participant_joined_no_resolved_location_stamps_turn_not_location() -> None:
    """AC5 (seam 2) — a router-named NPC seats even when the acting PC has no
    resolved location (the opponent is supplied explicitly, not via the location
    roster). ``party_location`` is then None: the stamp advances
    ``last_seen_turn`` but freezes ``last_seen_location`` (No Silent Fallbacks)."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=8),
    )
    # No character_locations entry for the acting PC → party_location is None.
    npc = _make_opponent_npc(location=_STALE_LOC, turn=_STALE_TURN)
    snap.npcs.append(npc)

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="social_duel",
        player_name=_PLAYER,
        npcs_present=[NpcMention(name=_OPPONENT, side="opponent")],  # router-named Other.
        genre_slug="tea_and_murder",
    )

    assert npc.last_seen_turn == 8, "turn must still advance when location is unresolved"
    assert npc.last_seen_location == _STALE_LOC, (
        "an unresolved location must NOT clobber the prior last_seen_location; "
        f"got {npc.last_seen_location!r}"
    )


def test_participant_joined_router_named_stamps_location(otel_capture) -> None:
    """AC3/AC4 variant — closes the 2×2 (seating-source × location-resolved) matrix
    on BOTH the object AND the span layer:
    a ROUTER-NAMED opponent (``npcs_present=[NpcMention(...)]``, not location
    fallback) WITH a resolved player location must stamp BOTH last_seen_turn and
    last_seen_location — and surface the WRITTEN location on the
    ``participant.joined`` span. Because the NPC starts at ``_STALE_LOC`` (≠ the
    player's hall), both the object assertion AND the span assertion are
    discriminating: a missing/stale location write fails. This is the span-layer
    location proof the co-located AC4 fixture cannot give (AC4 proves the span
    carries the discriminating TURN; this test proves it carries the
    discriminating LOCATION)."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=7),
    )
    snap.character_locations[_PLAYER] = _HALL
    # Prior location differs from the player's hall so the assertions prove the write.
    npc = _make_opponent_npc(location=_STALE_LOC, turn=_STALE_TURN)
    snap.npcs.append(npc)

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="social_duel",
        player_name=_PLAYER,
        npcs_present=[NpcMention(name=_OPPONENT, side="opponent")],  # router-named Other.
        genre_slug="tea_and_murder",
    )

    # Object layer.
    assert npc.last_seen_turn == 7, (
        f"router-named seated NPC must advance last_seen_turn to 7; got {npc.last_seen_turn}"
    )
    assert npc.last_seen_location == _HALL, (
        "router-named seating with a resolved player location must stamp "
        f"last_seen_location=_HALL; got {npc.last_seen_location!r}"
    )

    # Span layer (OTEL lie-detector) — discriminating because the NPC started at
    # _STALE_LOC, so the span carrying _HALL proves the WRITTEN value reached it.
    joined = [
        s for s in otel_capture.get_finished_spans() if s.name == "participant.joined"
    ]
    iain = [s for s in joined if (dict(s.attributes or {})).get("name") == _OPPONENT]
    assert iain, (
        "no participant.joined span for the router-named opponent; "
        f"names={[dict(s.attributes or {}).get('name') for s in joined]!r}"
    )
    attrs = dict(iain[0].attributes or {})
    assert attrs.get("last_seen_turn") == 7, (
        f"router-named participant.joined span wrong last_seen_turn; attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_location") == _HALL, (
        "router-named participant.joined span must carry the WRITTEN "
        f"last_seen_location=_HALL (NPC started at _STALE_LOC); attrs={sorted(attrs)!r}"
    )
