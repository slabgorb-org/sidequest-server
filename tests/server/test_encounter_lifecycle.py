from __future__ import annotations

import pytest

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader


@pytest.fixture
def cac_pack():
    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS).load("caverns_and_claudes")


def test_instantiate_combat_creates_encounter(cac_pack) -> None:
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=cac_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[NpcMention(name="Goblin", side="opponent", role="hostile")],
        genre_slug="caverns_and_claudes",
    )
    assert enc is not None
    assert snap.encounter is enc
    assert enc.encounter_type == "combat"
    actor_names = [a.name for a in enc.actors]
    assert "Rux" in actor_names
    assert "Goblin" in actor_names
    # caverns_and_claudes combat dual-dial: player_metric and opponent_metric.
    # Threshold 7 per ADR-093 calibration (was 10 pre-calibration).
    assert enc.player_metric.name == "momentum"
    assert enc.player_metric.starting == 0
    assert enc.player_metric.threshold == 7


def test_instantiate_unknown_type_raises(cac_pack) -> None:
    """CLAUDE.md: no silent fallback on unknown encounter_type."""
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    with pytest.raises(ValueError, match="unknown encounter_type"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=cac_pack,
            encounter_type="spelling_bee",
            player_name="Rux",
            npcs_present=[],
            genre_slug="caverns_and_claudes",
        )


def test_instantiate_replaces_resolved_encounter(cac_pack) -> None:
    """A resolved prior encounter does not block a new one."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.encounter import EncounterActor, EncounterMetric
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    prior = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="old", role="combatant", side="player")],
    )
    prior.resolved = True
    snap.encounter = prior
    # Story 45-33: combat now requires an opponent post-fallback. The original
    # test fixture passed npcs_present=[] because the focus is the resolved
    # encounter replacement, not opponent supply — adding an explicit
    # opponent preserves the test's intent.
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=cac_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[NpcMention(name="Goblin", side="opponent", role="hostile")],
        genre_slug="caverns_and_claudes",
    )
    assert snap.encounter is enc
    assert enc is not prior


def test_instantiate_active_encounter_is_noop(cac_pack) -> None:
    """If an active unresolved encounter already exists, do not clobber."""
    from sidequest.game.encounter import EncounterActor, EncounterMetric
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    active = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="already", role="combatant", side="player")],
    )
    snap.encounter = active
    result = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=cac_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[],
        genre_slug="caverns_and_claudes",
    )
    assert result is None
    assert snap.encounter is active


def test_resolve_from_trope_marks_resolved() -> None:
    from sidequest.game.encounter import EncounterActor, EncounterMetric
    from sidequest.server.dispatch.encounter_lifecycle import (
        resolve_encounter_from_trope,
    )

    snap = GameSnapshot(genre_slug="cac")
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="Rux", role="combatant", side="player")],
    )
    snap.encounter = enc
    result = resolve_encounter_from_trope(snapshot=snap, trope_id="last_stand")
    assert result is enc
    assert enc.resolved is True
    assert "last_stand" in (enc.outcome or "")


def test_resolve_from_trope_no_encounter_returns_none() -> None:
    from sidequest.server.dispatch.encounter_lifecycle import (
        resolve_encounter_from_trope,
    )

    snap = GameSnapshot(genre_slug="cac")
    assert resolve_encounter_from_trope(snapshot=snap, trope_id="x") is None


def test_resolve_from_trope_already_resolved_returns_none() -> None:
    from sidequest.game.encounter import EncounterActor, EncounterMetric
    from sidequest.server.dispatch.encounter_lifecycle import (
        resolve_encounter_from_trope,
    )

    snap = GameSnapshot(genre_slug="cac")
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="Rux", role="combatant", side="player")],
    )
    enc.resolved = True
    snap.encounter = enc
    assert resolve_encounter_from_trope(snapshot=snap, trope_id="x") is None


# ---------------------------------------------------------------------------
# Task 13: Dual dials + side-from-payload + invalid-side fail-loud
# ---------------------------------------------------------------------------


def test_instantiate_two_dials_from_cdef(snapshot_with_pack):
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap, pack = snapshot_with_pack
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
        genre_slug="test_pack",
    )
    assert enc is not None
    assert enc.player_metric.threshold == 10
    assert enc.opponent_metric.threshold == 10


def test_instantiate_routes_actor_sides_from_payload(snapshot_with_pack):
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap, pack = snapshot_with_pack
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[
            NpcMention(name="Promo", side="opponent", role="hostile"),
            NpcMention(name="Host", side="neutral", role="bystander"),
        ],
        genre_slug="test_pack",
    )
    sides = {a.name: a.side for a in enc.actors}
    assert sides["Sam"] == "player"
    assert sides["Promo"] == "opponent"
    assert sides["Host"] == "neutral"


def test_invalid_side_raises_with_span(snapshot_with_pack):
    """Invalid side at the lifecycle layer raises loudly.

    NpcMention.from_value validates side at narrator-extraction time. If a
    bypass path constructs an NpcMention directly with a bad side and reaches
    the lifecycle (e.g., via test fixture), we still fail loud.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap, pack = snapshot_with_pack
    # Bypass NpcMention.from_value: construct the dataclass directly with a
    # bad side. Validation happens at lifecycle entry.
    bad_npc = NpcMention(name="??", side="enemy", role="hostile")
    with pytest.raises(ValueError, match="declared_side|enemy"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="combat",
            player_name="Sam",
            npcs_present=[bad_npc],
            genre_slug="test_pack",
        )


# ──────────────────────────────────────────────────────────────────────────
# Multiplayer additional_player_names — playtest 2026-05-03 [BUG]
# ──────────────────────────────────────────────────────────────────────────


def test_instantiate_seats_additional_pcs_for_mp_bundle(cac_pack) -> None:
    """MP bundled turns must seat every PC, not just the action submitter.

    Pingpong 2026-05-03 [BUG]: confrontation widget showed only Scratchy
    (action submitter) when narrator initiated a negotiation that included
    Itchy as principal speaker. Both PCs played the bundled turn; both must
    appear in the actor list with side="player".
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=cac_pack,
        encounter_type="combat",
        player_name="Scratchy",
        npcs_present=[NpcMention(name="Inspector Volkova", side="opponent", role="hostile")],
        genre_slug="caverns_and_claudes",
        additional_player_names=["Itchy"],
    )
    assert enc is not None
    pc_names = {a.name for a in enc.actors if a.side == "player"}
    assert pc_names == {"Scratchy", "Itchy"}, (
        f"both bundled PCs must be seated as side=player; got {pc_names}"
    )


def test_instantiate_additional_pcs_dedup_against_primary(cac_pack) -> None:
    """If the caller passes the primary in additional list, dedup."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    # Story 45-33: combat requires an opponent post-fallback; this test's
    # focus is PC-list dedup, so supply a stub opponent and assert against
    # only the player-side actors.
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=cac_pack,
        encounter_type="combat",
        player_name="Scratchy",
        npcs_present=[NpcMention(name="Goblin", side="opponent", role="hostile")],
        genre_slug="caverns_and_claudes",
        additional_player_names=["Scratchy", "Itchy", "Itchy"],
    )
    assert enc is not None
    pc_names = [a.name for a in enc.actors if a.side == "player"]
    assert pc_names == ["Scratchy", "Itchy"], (
        f"duplicates and primary-in-extras must dedup; got {pc_names}"
    )


def test_instantiate_additional_pcs_default_none_keeps_solo_behavior(cac_pack) -> None:
    """Solo callers (additional_player_names=None) get a single-PC roster."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    # Story 45-33: combat requires an opponent. The test's focus is the
    # solo-PC roster shape (no MP bundle), not opponent supply.
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=cac_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[NpcMention(name="Goblin", side="opponent", role="hostile")],
        genre_slug="caverns_and_claudes",
    )
    assert enc is not None
    pc_names = [a.name for a in enc.actors if a.side == "player"]
    assert pc_names == ["Rux"]


# ──────────────────────────────────────────────────────────────────────────
# Story 45-33 — adversarial-review follow-ups on Story 45-18 (PR #98).
#
# Two correctness gaps the merged 45-18 work left exposed:
#
#   AC1 — Sealed-letter bypass guard (encounter_lifecycle.py:226) has zero
#         regression coverage. A regression that flipped `!=` to `==` or
#         dropped the guard would compile, lint, pass the existing 2685
#         tests, and silently break sealed-letter (commit-reveal) duels by
#         leaking bystander registry NPCs into the actor list.
#
#   AC2 — Empty narrator `npcs_present` + empty registry fallback for a
#         combat encounter currently produces ``actors=[player only]`` —
#         the exact Playtest 3 (Orin) bug shape 45-18 was meant to fix.
#         CLAUDE.md "No Silent Fallbacks" demands a loud failure here:
#         category=combat that resolves to zero opponents post-fallback
#         must raise (with an OTEL span) so the GM panel sees the lie.
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sealed_letter_pack():
    """Synthetic pack with one sealed-letter ConfrontationDef.

    Built locally so the test does not depend on space_opera content
    layout (the dogfight pack is the only on-disk sealed-letter today;
    binding to it would couple this regression test to genre content).
    Uses MagicMock(spec=GenrePack) for the same reason
    ``synthetic_two_dial_pack`` does in ``conftest.py``: only the
    ``rules.confrontations`` lookup is exercised.
    """
    from unittest.mock import MagicMock

    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        ConfrontationDef,
        InteractionCell,
        InteractionTable,
        MetricDef,
        ResolutionMode,
        RulesConfig,
    )

    table = InteractionTable(
        version="0.1.0",
        starting_state="merge",
        maneuvers_consumed=["straight", "loop"],
        cells=[
            InteractionCell(
                pair=["straight", "loop"],
                name="Blue scores",
                shape="passive vs offense",
                red_view={"target_bearing": "06", "closure": "opening", "gun_solution": False},
                blue_view={"target_bearing": "12", "closure": "opening", "gun_solution": True},
                narration_hint="Blue lines up the shot.",
            ),
        ],
    )
    from sidequest.genre.models.rules import BeatDef

    cdef = ConfrontationDef(
        type="duel",
        label="Sealed-Letter Duel",
        category="combat",
        resolution_mode=ResolutionMode.sealed_letter_lookup,
        player_metric=MetricDef(name="energy", starting=0, threshold=30),
        opponent_metric=MetricDef(name="energy", starting=0, threshold=30),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "straight",
                    "label": "Straight",
                    "kind": "push",
                    "stat_check": "DEX",
                }
            ),
        ],
        interaction_table=table,
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


@pytest.fixture
def combat_only_pack():
    """Synthetic pack with one beat-selection combat ConfrontationDef.

    Used to drive AC2 tests where the encounter category=combat must raise
    when no opponent can be sourced (neither narrator nor registry).
    """
    from unittest.mock import MagicMock

    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

    cdef = ConfrontationDef(
        type="brawl",
        label="Brawl",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ),
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


@pytest.fixture
def non_combat_pack():
    """Synthetic pack with one non-combat ConfrontationDef.

    AC2's no-opponent guard MUST be combat-only — non-combat encounters
    (negotiation, chase, social_duel) can legitimately resolve with a
    solo player while the narrator works the scene one-on-one.
    """
    from unittest.mock import MagicMock

    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

    cdef = ConfrontationDef(
        type="parley",
        label="Parley",
        category="social",
        player_metric=MetricDef(name="rapport", starting=0, threshold=10),
        opponent_metric=MetricDef(name="rapport", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "appeal",
                    "label": "Appeal",
                    "kind": "push",
                    "stat_check": "CHA",
                }
            ),
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


# ──────────────────────────────────────────────────────────────────────────
# AC1 — Sealed-letter bypass guard regression coverage
# ──────────────────────────────────────────────────────────────────────────


def test_sealed_letter_does_not_consume_registry_fallback(sealed_letter_pack):
    """Story 45-33 AC1 (primary).

    A sealed-letter encounter with one explicit opponent must NOT also pull
    a same-location NPC out of ``snapshot.npcs`` into the actor list. The
    location-scoped fallback (``_npc_fallback_at_location``, formerly
    ``_registry_fallback_npcs`` pre-45-52) is gated at
    ``encounter_lifecycle.py`` to non-sealed-letter modes precisely so
    the duel's 1-PC red / 1-NPC blue pairing stays inviolate.

    This test also asserts the OTEL ``encounter.confrontation_initiated``
    span carries ``actor_count=2`` so the GM panel can confirm the seal
    held end-to-end (CLAUDE.md OTEL principle).

    Regression catch: dropping the guard or flipping ``!=`` to ``==`` would
    let a bystander shadow the named opponent — the duel would proceed
    against the wrong actor and the existing 2685 tests would still pass.
    """
    import opentelemetry.trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.creature_core import (
        CreatureCore,
        HpPool,
        Inventory,
    )
    from sidequest.game.session import Npc
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    try:
        snap = GameSnapshot(genre_slug="test_pack")
        snap.character_locations["Maverick"] = "Hangar Bay 7"
        # Bystander at the same location — would be pulled into a non-sealed-letter
        # encounter via the location-scoped fallback. Must NOT be pulled into
        # the duel.
        snap.npcs.append(
            Npc(
                core=CreatureCore(
                    name="Deck Crew Chief",
                    description="A bystander.",
                    personality="Neutral.",
                    level=1,
                    xp=0,
                    inventory=Inventory(),
                    statuses=[],
                    hp=HpPool(current=10, max=10, base_max=10),
                ),
                npc_role_id="bystander",
                last_seen_location="Hangar Bay 7",
                last_seen_turn=2,
            )
        )
        explicit_opponent = NpcMention(
            name="Vulture",
            side="opponent",
            role="hostile",
        )

        enc = instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=sealed_letter_pack,
            encounter_type="duel",
            player_name="Maverick",
            npcs_present=[explicit_opponent],
            genre_slug="test_pack",
        )

        assert enc is not None
        actor_names = [a.name for a in enc.actors]
        assert "Maverick" in actor_names
        assert "Vulture" in actor_names
        assert "Deck Crew Chief" not in actor_names, (
            "bystander leaked into sealed-letter duel — "
            "the bypass guard in encounter_lifecycle.py regressed. "
            f"actors={actor_names!r}"
        )
        assert len(enc.actors) == 2, (
            f"sealed-letter duel must be exactly red+blue; got {actor_names!r}"
        )

        spans_by_name = {s.name: s for s in exporter.get_finished_spans()}
        init_span = spans_by_name.get("encounter.confrontation_initiated")
        assert init_span is not None, (
            f"expected encounter.confrontation_initiated span; got {sorted(spans_by_name)!r}"
        )
        attrs = dict(init_span.attributes or {})
        assert attrs.get("actor_count") == 2, (
            f"sealed-letter init must report actor_count=2; got {attrs.get('actor_count')!r}"
        )
    finally:
        processor.shutdown()


def test_sealed_letter_empty_npcs_present_raises_without_consuming_fallback(
    sealed_letter_pack,
):
    """Story 45-33 AC1 (defensive).

    When the narrator's ``npcs_present`` is EMPTY for a sealed-letter
    encounter AND a same-location NPC sits in ``snapshot.npcs``, the
    sealed-letter validator must raise "got 0 npcs_present". The
    location-scoped fallback must NOT be consulted — even in this
    degenerate empty path the bystander is not promoted into the duel
    as a substitute opponent.

    Regression catch: dropping the resolution_mode guard while keeping
    the validator length check would silently fall back, then pass the
    validator with the wrong NPC — the test above would catch the
    wrong-NPC path; this test catches the "fallback ran at all" path.
    """
    from sidequest.game.creature_core import (
        CreatureCore,
        HpPool,
        Inventory,
    )
    from sidequest.game.session import Npc
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="test_pack")
    snap.character_locations["Maverick"] = "Hangar Bay 7"
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Deck Crew Chief",
                description="A bystander.",
                personality="Neutral.",
                level=1,
                xp=0,
                inventory=Inventory(),
                statuses=[],
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            npc_role_id="bystander",
            last_seen_location="Hangar Bay 7",
            last_seen_turn=2,
        )
    )

    with pytest.raises(ValueError, match="got 0 npcs_present"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=sealed_letter_pack,
            encounter_type="duel",
            player_name="Maverick",
            npcs_present=[],
            genre_slug="test_pack",
        )

    # No encounter must have been written. If the guard regressed and the
    # fallback was consumed, the encounter would have been instantiated
    # with the bystander as the blue actor before any later rollback.
    assert snap.encounter is None, (
        f"snapshot.encounter must remain None when sealed-letter validator "
        f"rejects empty npcs_present; got {snap.encounter!r}"
    )


# ──────────────────────────────────────────────────────────────────────────
# AC2 — Empty + empty path must raise for combat (CLAUDE.md No Silent
# Fallbacks). Non-combat is allowed to remain a solo-actor encounter.
# ──────────────────────────────────────────────────────────────────────────


def test_combat_with_empty_npcs_and_empty_registry_fallback_raises(combat_only_pack):
    """Story 45-33 AC2 (primary).

    The Playtest 3 (Orin) bug shape: narrator emits ``confrontation=combat``
    with empty ``npcs_present``, the registry has no NPCs at the player's
    location (or no location at all), and the encounter is currently
    instantiated with ``actors=[player only]`` — a combat with nobody to
    fight.

    Per CLAUDE.md "No Silent Fallbacks", this must raise. A combat
    encounter with zero opponents post-fallback is a configuration
    failure — the pipeline should refuse to advance rather than emit a
    stub encounter that the dial subsystem will later silently drop
    opponent beats from.

    Currently failing: the function returns a player-only encounter
    instead of raising. Dev (green phase) adds the no-opponent guard.
    """
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="test_pack")
    snap.character_locations["Orin"] = "The Pit"
    # Registry is empty — the fallback returns []; combined with empty
    # narrator npcs_present this is the "empty + empty" Playtest 3 shape.

    with pytest.raises(ValueError, match=r"(?i)no opponent"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=combat_only_pack,
            encounter_type="brawl",
            player_name="Orin",
            npcs_present=[],
            genre_slug="test_pack",
        )

    assert snap.encounter is None, (
        f"snapshot.encounter must remain None when combat resolves to zero "
        f"opponents; got {snap.encounter!r}"
    )


def test_combat_with_no_location_and_empty_npcs_raises(combat_only_pack):
    """Story 45-33 AC2 (location=None variant).

    ``_registry_fallback_npcs`` short-circuits when ``snapshot.location``
    is falsy — returning ``[]``. Combined with empty narrator
    ``npcs_present``, this is the second known empty+empty path. Same
    expectation: combat with zero opponents must raise.
    """
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="test_pack")  # no location
    # No per-character location entry — party_location() returns None.
    assert snap.character_locations == {}

    with pytest.raises(ValueError, match=r"(?i)no opponent"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=combat_only_pack,
            encounter_type="brawl",
            player_name="Orin",
            npcs_present=[],
            genre_slug="test_pack",
        )


def test_combat_no_opponent_emits_otel_span(combat_only_pack):
    """Story 45-33 AC2 — OTEL lie-detector signal.

    Per CLAUDE.md OTEL principle: every backend fix that touches a
    subsystem must add an OTEL span the GM panel can read. The
    no-opponent guard fires on a real configuration failure; the GM
    panel needs to see ``encounter.no_opponent_available`` in the
    dashboard so Sebastien (the mechanical-first player) can confirm
    the guard engaged rather than the narrator improvising around an
    empty encounter.

    Span attributes required:
        - encounter_type
        - genre_slug
        - player_name
        - category (= "combat")
    """
    import opentelemetry.trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    try:
        snap = GameSnapshot(genre_slug="test_pack")
        snap.character_locations["Orin"] = "The Pit"
        with pytest.raises(ValueError, match=r"(?i)no opponent"):
            instantiate_encounter_from_trigger(
                snapshot=snap,
                pack=combat_only_pack,
                encounter_type="brawl",
                player_name="Orin",
                npcs_present=[],
                genre_slug="test_pack",
            )

        spans_by_name = {s.name: s for s in exporter.get_finished_spans()}
        no_opp_span = spans_by_name.get("encounter.no_opponent_available")
        assert no_opp_span is not None, (
            f"expected encounter.no_opponent_available span; got {sorted(spans_by_name)!r}"
        )
        attrs = dict(no_opp_span.attributes or {})
        assert attrs.get("encounter_type") == "brawl", (
            f"span missing encounter_type=brawl; attrs={sorted(attrs)!r}"
        )
        assert attrs.get("genre_slug") == "test_pack"
        assert attrs.get("player_name") == "Orin"
        assert attrs.get("category") == "combat", (
            f"no-opponent span must scope by category=combat; attrs={sorted(attrs)!r}"
        )
    finally:
        processor.shutdown()


def test_non_combat_with_empty_npcs_and_empty_registry_does_not_raise(non_combat_pack):
    """Story 45-33 AC2 (negative test — guard is combat-only).

    A social/parley encounter with no opponents is legitimate — the
    narrator may be opening a one-on-one negotiation where the NPC enters
    on a later beat, or running a scene of self-talk. The no-opponent
    guard must NOT fire for ``category != "combat"``; it would be a false
    alarm and would block legitimate non-combat scenes.

    Encounter is created with ``actors=[player only]`` and the existing
    ``encounter_empty_actor_list_span`` carries the (still legitimate)
    "narrator named no NPCs" signal.
    """
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(genre_slug="test_pack", location="Drawing Room")

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=non_combat_pack,
        encounter_type="parley",
        player_name="Ada",
        npcs_present=[],
        genre_slug="test_pack",
    )

    assert enc is not None
    actor_names = [a.name for a in enc.actors]
    assert actor_names == ["Ada"], (
        f"non-combat empty encounter should produce solo-player roster; got {actor_names!r}"
    )
