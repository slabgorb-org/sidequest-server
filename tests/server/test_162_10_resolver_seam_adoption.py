"""Story 162-10 (RED) — the unified resolver reaches the LAST four identity seams.

Story 162-2 landed ``resolve_roster_npc`` (canonical + alias + invented_from,
one diacritic-folding normalization) and cut the hp_depletion seeder + roster
conscription + inject dedup over to it, closing the two-names-one-enemy fork on
the showcase path. The Reviewer filed the remaining seams as non-blocking
follow-ups (sprint/archive/162-2-session.md:64,76,356,357): four seams still
match identity with a bare exact-string ``by_name`` dict / ``m.name == n`` /
``casefold`` and so re-open the same fork one seam over —

  1a. the narrator-mention path (``narration_apply._apply_npc_mentions`` Step 1:
      exact-casefold / comma / invented_from — NO diacritic fold, NO alias leg);
  1b. the seeder pool-member lookup (``m.name == actor.name``, exact);
  1c. the Fate opponent seeder (``by_name = {npc.core.name: npc}``, exact);
  1d. the dial edge-publish (``by_name.get(actor.name)``, exact).

And the telemetry gap (356): on the explicit ``npcs_present`` path
``participant.joined`` + the init span's ``combatant_names`` emit BEFORE the
seeder canonicalizes ``actor.name``, so the GM-panel lie-detector sees the stale
alias for one event.

WHAT THIS SUITE PINS (driving the REAL production seams; no source-text asserts):
  * an alias- or diacritic-named prose mention reconciles to the rostered NPC
    instead of minting a phantom pool duplicate (1a — RED);
  * an alias-named Fate opponent seats the canonical creature; a variant-named
    scene pool antagonist is PROMOTED, not fabricated as a stub (1b/1c — RED);
  * an alias-named dial opponent receives the published edge (1d — RED);
  * ``participant.joined`` + ``combatant_names`` carry the CANONICAL name (356 —
    RED);
  * (green guard) a diacritic-named roster creature seats via ASCII prose with no
    stub and stays reachable — the seat-seam integration 162-2 lacked (item 6);
  * (green guard) a case-variant head-check rebind emits NO ``identity.resolved``
    span — canonical-leg hits derive nothing (pins the item-5 comment fix).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterActor, EncounterMetric
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    FateConfig,
    ResolutionMode,
    RulesConfig,
)
from sidequest.server.dispatch.encounter_lifecycle import (
    _publish_combat_edge_to_npcs,
    _seed_combat_hp_depletion_to_npcs,
    _seed_fate_opponents,
    instantiate_encounter_from_trigger,
)
from sidequest.server.narration_apply import _apply_npc_mentions

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"
_LOC = "the_dropmouth"
_IDENTITY_RESOLVED_SPAN = "identity.resolved"
_PARTICIPANT_JOINED_SPAN = "participant.joined"
_INIT_SPAN = "encounter.confrontation_initiated"


# ---------------------------------------------------------------------------
# Shared fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture
def local_otel() -> Iterator[InMemorySpanExporter]:
    """A local in-memory exporter (self-contained; mirrors the fork-seating suite
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


def _core(name: str, *, hp: int = 24, ac: int = 15) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A statted adversary.",
        personality="Relentless.",
        inventory=Inventory(),
        hp=HpPool(current=hp, max=hp, base_max=hp),
        armor_class=ac,
    )


def _statted_npc(
    name: str,
    *,
    creature_id: str | None = None,
    hp: int = 24,
    aliases: list[str] | None = None,
    location: str | None = _LOC,
    disposition: int = -20,
) -> Npc:
    return Npc(
        core=_core(name, hp=hp),
        creature_id=creature_id,
        threat_level=1,
        disposition=disposition,
        aliases=aliases or [],
        last_seen_location=location,
        last_seen_turn=4,
    )


def _snapshot_with(*npcs: Npc, player: str = "Kirk") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[player] = _LOC
    snap.npcs.extend(npcs)
    return snap


def _combat_cdef() -> ConfrontationDef:
    strike = BeatDef.model_validate(
        {
            "id": "strike",
            "label": "Strike",
            "kind": "strike",
            "base": 2,
            "stat_check": "Strength",
            "damage_channel": "strike",
            "effect": "A blow.",
            "narrator_hint": "Hit.",
        }
    )
    return ConfrontationDef(
        type="combat",
        label="Skirmish",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition="hp_depletion",
        player_metric=None,
        opponent_metric=None,
        opponent_default_stats={"Strength": 10, "hp": 8, "armor_class": 12, "dexterity": 11},
        opponent_damage=DamageSpec(dice="1d6"),
        beats=[strike],
    )


_FATE_SKILLS = {"Shoot": 4, "Fight": 2, "Provoke": 3, "Athletics": 1, "Will": 2, "Notice": 3}


def _fate_pack() -> SimpleNamespace:
    return SimpleNamespace(
        rules=RulesConfig(ruleset="fate", fate=FateConfig(skills=dict(_FATE_SKILLS), refresh=3))
    )


# ---------------------------------------------------------------------------
# 1a. Mention path — alias + diacritic legs (narration_apply._apply_npc_mentions)
# ---------------------------------------------------------------------------


class TestMentionPathResolverAdoption:
    def test_mention_by_recorded_alias_hits_roster_not_phantom_mint(self) -> None:
        """RED: the mention path's Step-1 legs (exact / comma / invented_from) do
        not consult the alias ledger. A narrator cite by a RECORDED alias of a
        rostered NPC misses all three legs and mints a phantom culture-shuffled
        pool duplicate — the two-names-one-enemy fork the resolver exists to kill,
        alive one seam over. The unified resolver's alias leg reconciles it."""
        molgrath = _statted_npc(
            "Molgrath the Eyeless", creature_id="thief", aliases=["Hold-Dead, Still at the Shift"]
        )
        snap = _snapshot_with(molgrath)

        _apply_npc_mentions(
            snapshot=snap,
            mentions=[NpcMention(name="Hold-Dead, Still at the Shift", side="opponent")],
            turn_num=6,
        )

        assert molgrath.last_seen_turn == 6, (
            "a cite by the recorded alias must reconcile to the rostered NPC (npcs_hit)"
        )
        assert snap.npc_pool == [], (
            f"the alias cite minted a phantom pool duplicate instead of matching the "
            f"roster: {[m.name for m in snap.npc_pool]!r}"
        )

    def test_mention_by_diacritic_variant_hits_roster_not_phantom_mint(self) -> None:
        """RED: Step 1 folds case + comma + article but NOT diacritics
        (``casefold`` only, no ``fold_to_ascii``). Jade's perseus namer mints the
        canonical "Veyra Solnë"; the narrator later cites the ASCII "Veyra Solne"
        — casefold won't fold ``ë``, so the cite misses and double-mints. The
        resolver's single ``normalize_name`` (fold + casefold) is one identity."""
        veyra = _statted_npc("Veyra Solnë", creature_id="veyra")
        snap = _snapshot_with(veyra)

        _apply_npc_mentions(
            snapshot=snap,
            mentions=[NpcMention(name="Veyra Solne", side="neutral")],
            turn_num=7,
        )

        assert veyra.last_seen_turn == 7, (
            "the ASCII cite must reconcile to the diacritic-named rostered NPC"
        )
        assert snap.npc_pool == [], (
            f"diacritic drift double-minted the NPC: {[m.name for m in snap.npc_pool]!r}"
        )


# ---------------------------------------------------------------------------
# 1b + 1c. Fate opponent seeder — roster alias leg + pool-member variant leg
# ---------------------------------------------------------------------------


class TestFateSeederResolverAdoption:
    def _fate_snap(self, *npcs: Npc) -> GameSnapshot:
        snap = GameSnapshot(
            genre_slug="spaghetti_western",
            world_slug="coyote_star",
            turn_manager=TurnManager(interaction=5),
        )
        snap.character_locations["Reb"] = _LOC
        snap.npcs.extend(npcs)
        return snap

    def _seed(self, snap: GameSnapshot, opponent_name: str) -> None:
        _seed_fate_opponents(
            snapshot=snap,
            actors=[
                EncounterActor(name="Reb", role="lead", side="player"),
                EncounterActor(name=opponent_name, role="foe", side="opponent"),
            ],
            pack=_fate_pack(),  # type: ignore[arg-type]
            turn=5,
            acting_character_name="Reb",
        )

    def test_alias_named_fate_opponent_seats_canonical_no_twin(self) -> None:
        """RED (1c): the Fate seeder's ``by_name`` dict is exact on ``core.name``.
        An opponent named by a RECORDED alias of a rostered Fate adversary misses
        and fabricates an ephemeral stub beside the real one (two names, one
        enemy — Fate flavour). The resolver's alias leg attaches the sheet to the
        canonical creature instead."""
        el_lobo = _statted_npc("El Lobo", creature_id="lobo", aliases=["The Grey Wolf"])
        snap = self._fate_snap(el_lobo)

        self._seed(snap, "The Grey Wolf")

        assert len(snap.npcs) == 1, (
            f"the alias minted a Fate twin: {[n.core.name for n in snap.npcs]!r}"
        )
        assert not any(n.ephemeral for n in snap.npcs), "an ephemeral stub was fabricated beside it"
        assert el_lobo.core.fate_sheet is not None, "the sheet must land on the canonical creature"

    def test_variant_named_pool_antagonist_is_promoted_not_fabricated(self) -> None:
        """RED (1b): the pool-member promotion leg matches ``m.name == actor.name``
        exactly. A prior-turn narrated antagonist sitting in ``npc_pool`` under a
        diacritic name ("Doña Espina") is missed when the actor arrives ASCII
        ("Dona Espina") — so a hollow ephemeral stub is fabricated instead of
        promoting the established cast member. One normalization promotes it."""
        snap = self._fate_snap()
        snap.npc_pool.append(
            NpcPoolMember(name="Doña Espina", drawn_from="narrator_invented", is_creature=False)
        )

        self._seed(snap, "Dona Espina")

        assert not any(n.ephemeral for n in snap.npcs), (
            f"the pool antagonist was fabricated as a stub, not promoted: "
            f"{[(n.core.name, n.ephemeral) for n in snap.npcs]!r}"
        )
        assert len(snap.npcs) == 1, (
            f"expected the pool member promoted to a single backing Npc; got "
            f"{[n.core.name for n in snap.npcs]!r}"
        )


# ---------------------------------------------------------------------------
# 1d. Dial edge-publish — the alias-named opponent must receive the edge
# ---------------------------------------------------------------------------


class TestEdgePublishResolverAdoption:
    def test_alias_named_opponent_receives_published_edge(
        self, local_otel: InMemorySpanExporter
    ) -> None:
        """RED (1d): ``_publish_combat_edge_to_npcs`` builds an exact ``by_name``
        dict and ``continue``s on a miss. An opponent seated under a recorded
        alias never receives the dial-derived HP pool — its HP bar never updates
        and the ``npc.edge_published`` span never fires (a silent no-op). The
        resolver binds the edge to the canonical creature."""
        ghast = _statted_npc("Vellum Ghast", creature_id="ghast", hp=24, aliases=["The Pale King"])
        snap = _snapshot_with(ghast)

        _publish_combat_edge_to_npcs(
            snapshot=snap,
            actors=[EncounterActor(name="The Pale King", role="combatant", side="opponent")],
            opponent_metric=EncounterMetric(name="opponent", threshold=10, current=0),
            turn=5,
            source="dial_threshold",
            acting_character_name="Kirk",
        )

        assert ghast.core.hp.max == 10, (
            f"the alias-named opponent never received the published edge (dial "
            f"threshold=10); hp.max stayed {ghast.core.hp.max}"
        )
        edge_spans = [s for s in local_otel.get_finished_spans() if s.name == "npc.edge_published"]
        assert edge_spans, (
            "no npc.edge_published span fired — the edge silently no-op'd on the alias"
        )


# ---------------------------------------------------------------------------
# 356. participant.joined + combatant_names emit the CANONICAL name
# ---------------------------------------------------------------------------


class TestParticipantJoinedCanonicalTelemetry:
    def _instantiate_explicit(self, snap: GameSnapshot, opponent_name: str):
        """Drive the EXPLICIT npcs_present path (not materialized_threat): the
        caller names the opponent by an alias of a rostered creature."""
        return instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=load_genre_pack(_FIXTURE_PACK),
            encounter_type="combat",
            player_name="Kirk",
            npcs_present=[NpcMention(name=opponent_name, role="hostile", side="opponent")],
            genre_slug=snap.genre_slug,
        )

    def test_participant_joined_span_carries_canonical_name(
        self, local_otel: InMemorySpanExporter
    ) -> None:
        """RED: the participant.joined loop (and the seeder's canonicalization)
        run in that order — the span fires with the alias, THEN the seeder renames
        the actor to canonical. The GM-panel lie-detector sees "The Pale King"
        join and "Vellum Ghast" fight. Canonicalize before the membership event."""
        ghast = _statted_npc("Vellum Ghast", creature_id="ghast", aliases=["The Pale King"])
        snap = _snapshot_with(ghast)

        self._instantiate_explicit(snap, "The Pale King")

        joined = [
            dict(s.attributes or {})
            for s in local_otel.get_finished_spans()
            if s.name == _PARTICIPANT_JOINED_SPAN
        ]
        opp = [a for a in joined if a.get("side") == "opponent"]
        assert opp, f"no opponent-side participant.joined span fired; got {joined!r}"
        assert all(a.get("name") == "Vellum Ghast" for a in opp), (
            f"participant.joined emitted the stale alias pre-canonicalization: "
            f"{[a.get('name') for a in opp]!r}"
        )

    def test_init_span_combatant_names_are_canonical(
        self, local_otel: InMemorySpanExporter
    ) -> None:
        """RED: the init span's ``combatant_names`` is comma-joined from
        ``actor.name`` before the seeder canonicalizes — so it lists the alias."""
        ghast = _statted_npc("Vellum Ghast", creature_id="ghast", aliases=["The Pale King"])
        snap = _snapshot_with(ghast)

        self._instantiate_explicit(snap, "The Pale King")

        init = [
            dict(s.attributes or {})
            for s in local_otel.get_finished_spans()
            if s.name == _INIT_SPAN
        ]
        assert init, "no encounter.confrontation_initiated span fired"
        names = " | ".join(str(a.get("combatant_names", "")) for a in init)
        assert "The Pale King" not in names, (
            f"combatant_names carried the stale alias pre-canonicalization: {names!r}"
        )
        assert "Vellum Ghast" in names, (
            f"combatant_names should list the canonical opponent name; got {names!r}"
        )


# ---------------------------------------------------------------------------
# Item 6 (green guard) — diacritic seat-seam integration: 162-2 tested
# normalize_name at the unit level; this exercises the full resolve -> seat ->
# HP-seed -> reachability path with an ASCII prose query against a diacritic
# roster name. Resolves on the resolver's CANONICAL leg (fold-equal), so it
# derives nothing and emits NO identity.resolved span (guarded below) — that is
# the correct behavior, and it is why AC6's "identity.resolved fires" expectation
# is a bug (see Delivery Findings): a pure diacritic-vs-canonical match is leg-1
# silent, not an alias derivation.
# ---------------------------------------------------------------------------


class TestDiacriticSeatIntegration:
    def test_diacritic_roster_creature_seats_via_ascii_prose_no_stub(
        self, local_otel: InMemorySpanExporter
    ) -> None:
        veyra = _statted_npc("Veyra Solnë", creature_id="veyra", hp=24)
        snap = _snapshot_with(veyra)
        actor = EncounterActor(name="Veyra Solne", role="combatant", side="opponent")

        _seed_combat_hp_depletion_to_npcs(
            snapshot=snap,
            actors=[actor],
            cdef=_combat_cdef(),
            turn=5,
            source="encounter_handshake",
            acting_character_name="Kirk",
            ruleset=get_ruleset_module("wwn"),
        )

        # No stub; roster did not grow.
        assert len(snap.npcs) == 1, (
            f"the ASCII prose query minted a diacritic twin: {[n.core.name for n in snap.npcs]!r}"
        )
        assert not any(n.ephemeral for n in snap.npcs)
        # The seat is CANONICALIZED and reachable by every find_creature_core consumer.
        assert actor.name == "Veyra Solnë", f"seat left under the ASCII query {actor.name!r}"
        assert snap.find_creature_core(actor.name) is not None
        assert veyra.core.hp.max == 24, "the bound creature's statted HP must survive (108-2)"
        # Canonical leg derives nothing — no identity.resolved span (alias/invented
        # only). Pins that diacritic folding is NOT an alias derivation.
        resolved = [s for s in local_otel.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN]
        assert resolved == [], (
            "a diacritic-vs-canonical match resolves on the canonical leg and must "
            f"emit NO identity.resolved span; got {[dict(s.attributes or {}) for s in resolved]!r}"
        )


# ---------------------------------------------------------------------------
# Item 5 (green guard) — the head-check case-variant rebind emits NO
# identity.resolved span. Pins the behavior the corrected comment must describe
# (encounter_lifecycle.py head-check block): canonical-leg hits derive nothing.
# ---------------------------------------------------------------------------


class TestCaseVariantRebindEmitsNoSpan:
    def test_case_variant_head_check_rebind_is_span_silent(
        self, local_otel: InMemorySpanExporter
    ) -> None:
        """The router names a CASE variant of a rostered creature. The head-check
        rebind fires (``known.core.name != materialized_threat.name``) and rewrites
        the seat to canonical — but the resolver matched on the canonical leg, so
        NO identity.resolved span is emitted. The old comment claimed the span
        makes this rebind observable; it does not for the case-variant leg."""
        ghast = _statted_npc("Vellum Ghast", creature_id="ghast")
        snap = _snapshot_with(ghast)

        enc = instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=load_genre_pack(_FIXTURE_PACK),
            encounter_type="combat",
            player_name="Kirk",
            npcs_present=[],
            genre_slug=snap.genre_slug,
            materialized_threat=NpcMention(name="vellum ghast", role="hostile", side="opponent"),
        )

        opponents = [a.name for a in enc.actors if a.side == "opponent"]
        assert opponents == ["Vellum Ghast"], (
            f"case-variant threat did not seat canonically: {opponents!r}"
        )
        resolved = [s for s in local_otel.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN]
        assert resolved == [], (
            "the case-variant rebind matched the canonical leg — it must emit NO "
            f"identity.resolved span; got {[dict(s.attributes or {}) for s in resolved]!r}"
        )
