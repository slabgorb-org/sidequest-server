"""Story 45-18 — encounter.actors registers all combatants, not just player.

Wire-first RED tests for the bug discovered in Playtest 3 (Orin save):
6 rounds of combat with a "Crawling Scavenger" but ``encounter.actors``
contained only the player. Per-actor damage and per-side momentum tracking
were impossible because the opponent was never registered as a combatant.

These tests exercise the production wire end-to-end:

    snapshot.npcs (scene NPCs, post-Wave-2A canonical store)
        + narrator confrontation trigger
            → narration_apply._apply_narration_result_to_snapshot
                → encounter_lifecycle.instantiate_encounter_from_trigger
                    → snapshot.encounter.actors  ← MUST contain all combatants

Each test starts from the Playtest 3 shape: the narrator emits
``confrontation="combat"`` with an EMPTY ``npcs_present`` list (the
extraction dropped the adversary), but ``snapshot.npcs`` already has the
opponent recorded at the player's current location from prior turns
(Wave 2A pool/state split — the legacy ``npc_registry`` is gone as of
story 45-52). The handshake must register that opponent as an
EncounterActor.

References:
- AC1: handshake registers every combatant (player + each active NPC)
- AC2: per-actor damage + per-side momentum tracking actually works
- AC3: encounter init emits OTEL span carrying actor_count + combatant_names
- AC4: regression — Crawling Scavenger / Orin / 6 rounds of combat
- AC5: no orphan actor mutation sites in production code

Storage convention: tests load the frozen ``test_genre`` fixture pack
directly from disk via ``load_genre_pack`` to bypass the session-wide
GenreLoader cache (see test_encounter_apply_narration.py for the rationale).
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

from sidequest.agents.orchestrator import (
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
)
from sidequest.game.creature_core import (
    CreatureCore,
    Inventory,
    placeholder_edge_pool,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for
from tests._helpers.trigger_encounter import trigger_encounter

# Frozen fixture pack — same trick used in test_encounter_apply_narration.py
# to dodge the session-wide GenreLoader cache.
_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


def _load_pack():
    return load_genre_pack(_FIXTURE_PACK)


def _make_npc(
    name: str,
    *,
    role: str | None = None,
    pronouns: str | None = None,
    appearance: str | None = None,
    last_seen_location: str | None = None,
    last_seen_turn: int = 0,
) -> Npc:
    """Build a minimal stateful ``Npc`` for fallback-source fixtures.

    Story 45-52: the legacy ``NpcRegistryEntry`` fallback source moved to
    ``snapshot.npcs`` with ``last_seen_location`` driving the
    location-scoped match. ``Npc`` requires a CreatureCore + edge pool; the
    placeholder pool is fine here — the tests assert on actor registration,
    not edge values.
    """
    return Npc(
        core=CreatureCore(
            name=name,
            description=appearance or "An NPC.",
            personality="Neutral.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            edge=placeholder_edge_pool(),
        ),
        pronouns=pronouns,
        appearance=appearance,
        npc_role_id=role,
        last_seen_location=last_seen_location,
        last_seen_turn=last_seen_turn,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def playtest3_snapshot():
    """Snapshot reproducing the Playtest 3 [Orin save] scene shape:

    - Player is at "Mawdeep Caverns".
    - ``snapshot.npcs`` already lists "Crawling Scavenger" at that same
      location from a prior turn (the narrator introduced the creature
      narratively before combat began). Story 45-52 cleanup: this used
      to live on the legacy ``npc_registry``; canonical home now is
      ``snapshot.npcs`` with ``last_seen_location`` driving the
      location-scoped fallback.
    - No active encounter yet — the next narration turn will trigger one.

    The next narration turn emits ``confrontation="combat"`` with an EMPTY
    ``npcs_present`` (the JSON extraction dropped the adversary, exactly as
    observed in the save). The handshake must still register the opponent.
    """
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=4),
    )
    # Wave 2B: per-character location replaces the party-level field.
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    snap.npcs.append(
        _make_npc(
            "Crawling Scavenger",
            role="hostile",
            pronouns="it/its",
            appearance="a chittering carapaced thing the size of a hound",
            last_seen_location="Mawdeep Caverns",
            last_seen_turn=3,
        )
    )
    pack = _load_pack()
    return snap, pack


@pytest.fixture
def otel_capture():
    """Attach an in-memory exporter to the running TracerProvider.

    Mirrors the otel_capture fixture in test_encounter_apply_narration.py.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()  # idempotent
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


# ---------------------------------------------------------------------------
# AC1 — Encounter-start handshake registers all combatants
# ---------------------------------------------------------------------------


def test_handshake_registers_npc_from_registry_when_npcs_present_empty(
    playtest3_snapshot,
):
    """Playtest 3 reproduction.

    Given a player at "Mawdeep Caverns" and a Crawling Scavenger already in
    ``snapshot.npcs`` at the same location (post-Wave-2A canonical home for
    fallback-source NPCs), when the narrator fires a confrontation with
    empty ``npcs_present`` (dropped extraction), the encounter-start
    handshake MUST still register the Crawling Scavenger as an opponent
    EncounterActor. Otherwise per-actor tracking is impossible and combat
    plays out as a one-sided dial advance for 6+ rounds.
    """
    snap, pack = playtest3_snapshot
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    enc = snap.encounter
    assert enc is not None, "encounter was not instantiated"
    actor_names = [a.name for a in enc.actors]
    assert "Orin" in actor_names, f"player not registered (actors={actor_names!r})"
    assert "Crawling Scavenger" in actor_names, (
        "opponent NPC was not registered from snapshot.npcs — "
        "handshake regressed to player-only actors. "
        f"actors={actor_names!r}"
    )


def test_handshake_registers_multiple_npcs_from_registry(playtest3_snapshot):
    """Two opponents at the same location are both registered."""
    snap, pack = playtest3_snapshot
    snap.npcs.append(
        _make_npc(
            "Dust Strider",
            role="hostile",
            last_seen_location="Mawdeep Caverns",
            last_seen_turn=3,
        )
    )
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    enc = snap.encounter
    assert enc is not None
    actor_names = {a.name for a in enc.actors}
    assert {"Orin", "Crawling Scavenger", "Dust Strider"} <= actor_names, (
        f"missing combatants: actors={sorted(actor_names)!r}"
    )


def test_handshake_skips_registry_npcs_at_other_locations(playtest3_snapshot):
    """An NPC last seen at a different location is NOT pulled into combat
    in the Mawdeep Caverns. Otherwise the fallback would over-register.
    """
    snap, pack = playtest3_snapshot
    snap.npcs.append(
        _make_npc(
            "Brother Halrik",
            role="merchant",
            last_seen_location="Highvale Market",  # different location
            last_seen_turn=2,
        )
    )
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    enc = snap.encounter
    assert enc is not None
    actor_names = {a.name for a in enc.actors}
    assert "Crawling Scavenger" in actor_names
    assert "Brother Halrik" not in actor_names, (
        "an NPC last seen elsewhere was incorrectly pulled into the encounter"
    )


def test_handshake_prefers_explicit_npcs_present_when_provided(
    playtest3_snapshot,
):
    """When the narrator DOES supply ``npcs_present``, those names are used
    and the location-scoped fallback is not over-pulled. The fallback is
    for the Playtest 3 shape (empty extraction), not for replacing the
    explicit list.
    """
    snap, pack = playtest3_snapshot
    trigger_encounter(
        snap, pack, "combat", "Orin",
        npcs_present=[
            NpcMention(
                name="Goblin",
                side="opponent",
                role="hostile",
            ),
        ],
    )
    enc = snap.encounter
    assert enc is not None
    actor_names = {a.name for a in enc.actors}
    # Explicit Goblin is registered.
    assert "Goblin" in actor_names
    assert "Orin" in actor_names


# ---------------------------------------------------------------------------
# AC2 — Per-actor damage + per-side momentum tracking becomes operational
# ---------------------------------------------------------------------------


def test_per_actor_state_isolated_for_player_and_opponent_after_handshake(
    playtest3_snapshot,
):
    """Once both actors are registered via the handshake, per_actor_state
    mutations on each actor stay isolated. This is the per-actor damage
    storage substrate: with only [Orin] in actors there is nowhere to record
    the Crawling Scavenger's damage.
    """
    snap, pack = playtest3_snapshot
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    enc = snap.encounter
    assert enc is not None

    orin = enc.find_actor("Orin")
    scav = enc.find_actor("Crawling Scavenger")
    assert orin is not None and scav is not None, (
        f"expected both actors registered; got {[a.name for a in enc.actors]!r}"
    )

    # Record damage on each actor independently.
    orin.per_actor_state["damage"] = 3
    scav.per_actor_state["damage"] = 7

    # Mutations must NOT bleed across actors (catches shared-default-dict).
    assert orin.per_actor_state["damage"] == 3
    assert scav.per_actor_state["damage"] == 7
    assert orin.per_actor_state is not scav.per_actor_state


def test_opponent_beat_advances_opponent_metric_after_handshake(
    playtest3_snapshot,
):
    """The Crawling Scavenger's attack beat must reach apply_beat — which
    requires the actor to be in ``encounter.actors``. Pre-fix, the narrator's
    opponent-side beat raised "unknown actor" because actors=[Orin only],
    leaving opponent_metric stuck at 0 for 6 rounds.

    This is the per-side momentum tracking AC: each side's dial advances
    when its actor's beat fires.
    """
    snap, pack = playtest3_snapshot

    # Step 1: encounter starts with both actors registered.
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    assert snap.encounter is not None

    # Step 2: an opponent-side beat (Crawling Scavenger attacks). Pre-fix
    # this raises ValueError("unknown actor 'Crawling Scavenger'") because
    # the actor wasn't registered.
    opp_turn = NarrationTurnResult(
        narration="The scavenger gores Orin's flank.",
        beat_selections=[
            BeatSelection(
                actor="Crawling Scavenger",
                beat_id="attack",
                outcome=RollOutcome.Success,
            ),
        ],
        npcs_present=[],
    )
    _apply_narration_result_to_snapshot(
        snap,
        opp_turn,
        player_name="Orin",
        pack=pack,
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.opponent_metric.current > 0, (
        "opponent_metric never advanced — opponent's beat was dropped or "
        "raised because the actor wasn't in encounter.actors. "
        f"opponent_metric={enc.opponent_metric.current}"
    )


def test_per_side_metrics_track_player_and_opponent_independently(
    playtest3_snapshot,
):
    """Player attacks → player_metric advances. Opponent attacks →
    opponent_metric advances. The two dials must not be co-mingled and
    BOTH must advance when their respective actor's beat fires.
    """
    snap, pack = playtest3_snapshot

    # Start the encounter.
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    assert snap.encounter is not None

    # Player attacks (explicit-action path so the SOUL gate doesn't drop it).
    player_turn = NarrationTurnResult(
        narration="Orin swings.",
        beat_selections=[
            BeatSelection(
                actor="Orin",
                beat_id="attack",
                outcome=RollOutcome.Success,
            ),
        ],
        npcs_present=[],
    )
    _apply_narration_result_to_snapshot(
        snap,
        player_turn,
        player_name="Orin",
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
    )
    player_after = snap.encounter.player_metric.current
    opp_baseline = snap.encounter.opponent_metric.current

    assert player_after > 0, f"player_metric did not advance ({player_after})"

    # Opponent attacks (narrator path, opponent-side beats are not gated).
    opp_turn = NarrationTurnResult(
        narration="The scavenger lunges.",
        beat_selections=[
            BeatSelection(
                actor="Crawling Scavenger",
                beat_id="attack",
                outcome=RollOutcome.Success,
            ),
        ],
        npcs_present=[],
    )
    _apply_narration_result_to_snapshot(
        snap,
        opp_turn,
        player_name="Orin",
        pack=pack,
        room=room_for(snap),
    )
    opp_after = snap.encounter.opponent_metric.current

    assert opp_after > opp_baseline, (
        "opponent_metric did not advance after opponent's beat — opponent "
        "actor wasn't reachable on the encounter, or the beat was silently "
        f"dropped. before={opp_baseline} after={opp_after}"
    )

    # Player's dial unaffected by the opponent's beat (no co-mingling).
    assert snap.encounter.player_metric.current == player_after, (
        "opponent's beat leaked into the player's dial"
    )


# ---------------------------------------------------------------------------
# AC3 — Encounter init emits OTEL span with actor_count + combatant_names
# ---------------------------------------------------------------------------


def test_encounter_init_span_carries_actor_count_and_combatant_names(
    playtest3_snapshot,
    otel_capture: InMemorySpanExporter,
):
    """The GM panel needs to verify the fix is working by reading an OTEL
    span on encounter init. The span must include:

    - ``actor_count`` — distinct combatants registered
    - ``combatant_names`` — list/string of registered names

    Without these attributes Keith can't tell from the dashboard whether
    the actors array got populated or not.
    """
    snap, pack = playtest3_snapshot
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}
    init_span = spans_by_name.get("encounter.confrontation_initiated")
    assert init_span is not None, (
        f"expected encounter.confrontation_initiated span; got {sorted(spans_by_name)!r}"
    )

    attrs = dict(init_span.attributes or {})
    assert "actor_count" in attrs, f"span missing actor_count attribute; attrs={sorted(attrs)!r}"
    assert int(attrs["actor_count"]) == 2, (
        f"actor_count should be 2 (Orin + Crawling Scavenger); got {attrs['actor_count']!r}"
    )

    # combatant_names — accept list or comma-joined string for OTEL
    # serialization flexibility, but the names must both be present.
    raw_names = attrs.get("combatant_names")
    assert raw_names is not None, f"span missing combatant_names attribute; attrs={sorted(attrs)!r}"
    if isinstance(raw_names, (list, tuple)):
        names_blob = ",".join(str(n) for n in raw_names)
    else:
        names_blob = str(raw_names)
    assert "Orin" in names_blob
    assert "Crawling Scavenger" in names_blob


# ---------------------------------------------------------------------------
# AC4 — Regression: Playtest 3 Orin + Crawling Scavenger, 6 rounds
# ---------------------------------------------------------------------------


def test_six_round_combat_keeps_named_npc_in_actors(playtest3_snapshot):
    """Mirror Playtest 3: Orin vs Crawling Scavenger, 6 rounds of combat.
    Throughout, ``encounter.actors`` must explicitly contain "Crawling
    Scavenger" by name (not just [Orin]).

    Pre-fix: actors stayed [Orin] for all 6 rounds.
    """
    snap, pack = playtest3_snapshot

    # Round 0: encounter starts.
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    assert snap.encounter is not None
    enc = snap.encounter

    # Six rounds of alternating beats. The narrator emits empty
    # ``npcs_present`` every round (Playtest 3 shape).
    for round_idx in range(6):
        if enc.resolved:
            break
        # Player attacks (explicit-action so the SOUL gate doesn't drop it).
        _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration=f"Round {round_idx}: Orin strikes.",
                beat_selections=[
                    BeatSelection(
                        actor="Orin",
                        beat_id="defend",  # small base, slow
                        outcome=RollOutcome.Success,
                    ),
                ],
                npcs_present=[],
            ),
            player_name="Orin",
            pack=pack,
            from_explicit_action=True,
            room=room_for(snap),
        )
        if enc.resolved:
            break
        # Scavenger attacks back.
        _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration=f"Round {round_idx}: the scavenger lunges.",
                beat_selections=[
                    BeatSelection(
                        actor="Crawling Scavenger",
                        beat_id="defend",
                        outcome=RollOutcome.Success,
                    ),
                ],
                npcs_present=[],
            ),
            player_name="Orin",
            pack=pack,
            room=room_for(snap),
        )

        # The bug: by some round, actors regressed to [Orin]. Assert here.
        names = [a.name for a in enc.actors]
        assert "Orin" in names
        assert "Crawling Scavenger" in names, (
            f"round {round_idx}: actors regressed to {names!r} — "
            f"the Crawling Scavenger fell out of the encounter."
        )

    # Final invariant: even if the encounter resolved early, the named NPC
    # must still be in actors (not silently scrubbed).
    final_names = [a.name for a in enc.actors]
    assert "Crawling Scavenger" in final_names, (
        f"final actors missing the named NPC: {final_names!r}"
    )


# ---------------------------------------------------------------------------
# AC5 — Wiring: handshake is the single registration site
# ---------------------------------------------------------------------------


def test_no_orphan_actors_assignment_in_production_code():
    """Per AC5: there must be no production code path that constructs a
    ``StructuredEncounter`` with an explicit ``actors=`` argument outside
    of the canonical handshake (``encounter_lifecycle.py``). The handshake
    is the sole site allowed to *register* actors — every other consumer
    of ``StructuredEncounter`` must read or mutate an existing encounter,
    not build one from scratch.

    Uses AST parsing so docstring / comment mentions of the class don't
    trigger false positives.
    """
    import ast

    server_root = Path(__file__).resolve().parents[2] / "sidequest"
    offenders: list[str] = []

    for py in server_root.rglob("*.py"):
        # Allowed construction sites: the canonical handshake module.
        if py.name == "encounter_lifecycle.py":
            continue

        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != "StructuredEncounter":
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "actors" in kw_names:
                offenders.append(f"{py.relative_to(server_root.parent)}:{node.lineno}")

    assert not offenders, (
        "Production code constructs StructuredEncounter with an explicit "
        "``actors=`` argument outside of encounter_lifecycle.py. The "
        "handshake must be the single site that registers actors. "
        f"offenders={offenders!r}"
    )
