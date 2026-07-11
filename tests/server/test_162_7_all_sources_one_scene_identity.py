"""Story 162-7 — the all-sources-one-scene identity regression guard.

Epic 162 consolidated NPC/creature identity onto an id-keyed surface: a typed
``Origin`` (``sidequest/game/origin.py``), the ``identity_key`` precedence
(authored id > creature id > normalized name), one shared roster lookup
(``resolve_roster_npc``), and the ``Npc.aliases`` ledger — so the narrator can
call ``creature_id="thief"`` "Molgrath the Eyeless" in prose without forking
identity (the 108-2 two-names-one-enemy report). 162-1 supplied content-version
keying, 162-2 the IdentityKey + alias ledger, 162-3 the generics last resort.

This suite is the **permanent regression guard** ADR-156 §"Relationship to the
epic" names for 162-7: assert **one identity per creature** with the real spawn
seams firing in one scene, driving *production functions* (No Source-Text Wiring
Tests — CLAUDE.md). It is largely green-on-write: it guards the shipped 162-2/-3
behavior against future regression. The genuine RED→GREEN work of this story is
the sibling understudy detector (the naive-player-visible symptom).

SCOPE — read before extending:

* The invariant is a **materialized-layer** property. It holds over
  ``snapshot.npcs`` (where ``identity_key``/``resolve_roster_npc`` operate). The
  three pool-staging sources (narrator mentions, prose extraction, zone-cast)
  emit ``NpcPoolMember``s, which carry no ``creature_id`` by design (ADR-118
  identity-only staging) — they reconcile to an id on promotion, not at the pool
  tier.
* The **Green Room single-gate materializer (ADR-156) is NOT built** — it is a
  proposed design whose implementation stories are unfiled. There is no
  ``admit()`` to call; the seven feeders still append independently. This test
  drives the real seams directly and asserts the id-keyed identity primitives
  hold where 162-2 wired them (the seeder, the inject dedup, the authored
  preload, the alias ledger).
* Four seams remain name-string-keyed, deferred to story **162-10** (unified
  resolver adoption at the mention path, the Fate seeder, edge-publish, and the
  pool-member lookup). This suite does NOT assert those don't fork — converting
  them is 162-10's scope, not this 2-point guard's. The gap is filed as a
  Delivery Finding, not quarantined (the server repo's rule: "never xfail
  in-flight features"). ``TestIdentityKeyConvergesAcrossOriginKinds`` documents
  *why* ids matter (a name-only mint is exactly the fork those seams still
  produce).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.alias_accretion import accrete_npc_aliases
from sidequest.game.encounter import EncounterActor
from sidequest.game.origin import (
    Origin,
    OriginKind,
    derive_origin,
    identity_key,
    resolve_roster_npc,
)
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.session import GameSnapshot, NpcPatch
from sidequest.game.turn import TurnManager
from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, ResolutionMode
from sidequest.server.dispatch import monster_manual_inject

_IDENTITY_RESOLVED_SPAN = "identity.resolved"


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """In-memory span capture (same shape as test_162_2_identity_fork_seating)."""
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


def _combat_cdef() -> ConfrontationDef:
    """Minimal hp_depletion combat cdef (mirrors the 162-2 seeder fixture)."""
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


def _seed_opponent(snap: GameSnapshot, opponent_name: str) -> None:
    """Drive the REAL converted opponent-seater (spawn path #4)."""
    from sidequest.server.dispatch.encounter_lifecycle import (
        _seed_combat_hp_depletion_to_npcs,
    )

    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=[EncounterActor(name=opponent_name, role="combatant", side="opponent")],
        cdef=_combat_cdef(),
        turn=5,
        source="encounter_handshake",
        acting_character_name="Kirk",
        ruleset=get_ruleset_module("wwn"),
    )


def _inject_room_creature(
    monkeypatch: pytest.MonkeyPatch,
    *,
    patches: list[NpcPatch],
    location: str = "entrance",
) -> GameSnapshot:
    """Drive the REAL MM injection seam (spawn path #1) with the room-binding
    builder stubbed at its module seam (its content plumbing is covered
    elsewhere; here we only need it to feed the injector real ``NpcPatch``es so
    the id-keyed dedup + materialization run for real)."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Kirk"] = location
    sd = SimpleNamespace(monster_manual=None, genre_pack=None, world_slug="beneath_sunden")
    monkeypatch.setattr(
        monster_manual_inject,
        "_npc_patches_for_room_binding",
        lambda *a, **k: list(patches),
    )
    monkeypatch.setattr(
        monster_manual_inject,
        "_npc_patches_for_region_population",
        lambda *a, **k: [],
    )
    monster_manual_inject.inject(
        sd,  # type: ignore[arg-type]
        snap,
        current_location=location,
        in_combat=False,
        room_id=location,
    )
    return snap


# ---------------------------------------------------------------------------
# 1. identity_key collapses one creature across every origin kind (the invariant
#    every feeder's product must satisfy). Pure-primitive contract guard: green
#    on arrival by design — it pins the mechanism the wiring tests rely on.
# ---------------------------------------------------------------------------


class TestIdentityKeyConvergesAcrossOriginKinds:
    def test_same_creature_id_is_one_identity_from_every_source(self) -> None:
        """A creature can be proposed by many feeders under many display names.
        As long as each stamps the ``creature_id``, they collapse to ONE
        identity key regardless of source tier or prose name — the structural
        cure for §4 conflict #1 (N-source convergence).

        GENERIC is DELIBERATELY excluded from this kinds list (ADR-156
        Amendment B, Green Room task 1, 2026-07-11): a ``generics:`` bestiary
        row is a stat DONOR shared by many seatings, not an identity — "Gruk
        the Smasher" and "Grave-Thief" both drawn from ``creature_id="thief"``
        are two DIFFERENT people who happen to share a chassis, and must NOT
        converge into one identity. ``identity_key`` keys GENERIC by name for
        exactly this reason (see sidequest.game.origin.identity_key and
        tests/game/test_162_3_bestiary_generics_schema.py::
        TestGenericOriginKind::test_identity_key_for_generic_origin_is_name_keyed)."""
        kinds = [
            OriginKind.MANUAL_POOL,
            OriginKind.REGION_POPULATION,
            OriginKind.ROOM_BOUND,
            OriginKind.NARRATOR_INVENTED,
            OriginKind.EPHEMERAL_STUB,
        ]
        names = ["Thief", "thief", "A Thief", "Molgrath", "x"]
        keys = {
            identity_key(Origin(kind=k, creature_id="thief"), display)
            for k, display in zip(kinds, names, strict=True)
        }
        assert keys == {"creature:thief"}, (
            f"same creature_id forked across origin kinds/names: {keys!r}"
        )

    def test_two_prose_names_one_creature_collapse_by_id(self) -> None:
        """The literal 108-2 fork: the engine's ``creature_id="thief"`` and the
        narrator's "Molgrath the Eyeless". Keyed by id, they are one identity."""
        engine = identity_key(Origin(kind=OriginKind.MANUAL_POOL, creature_id="thief"), "Thief")
        prose = identity_key(
            Origin(kind=OriginKind.NARRATOR_INVENTED, creature_id="thief"),
            "Molgrath the Eyeless",
        )
        assert engine == prose == "creature:thief"

    def test_authored_id_outranks_creature_id(self) -> None:
        """Precedence: an authored placement owns identity over a bestiary id
        (ADR-156 §3 IdentityKey order)."""
        key = identity_key(
            Origin(kind=OriginKind.AUTHORED, authored_id="molgrath", creature_id="thief"),
            "Molgrath the Eyeless",
        )
        assert key == "authored:molgrath"

    def test_idless_mints_fork_by_name(self) -> None:
        """WHY the epic drives ids into every feeder: two narrator mints with no
        id key on the normalized NAME, so different prose strings for one being
        fork — exactly what the four unconverted seams (162-10) still produce.
        This is the failure the id-keyed surface exists to delete."""
        a = identity_key(Origin(kind=OriginKind.NARRATOR_INVENTED), "Molgrath the Eyeless")
        b = identity_key(Origin(kind=OriginKind.NARRATOR_INVENTED), "The Eyeless One")
        assert a != b
        assert a.startswith("name:") and b.startswith("name:")


# ---------------------------------------------------------------------------
# 2. All-sources-one-scene wiring: MM injection (#1) materializes a bound
#    creature; the narrator renames it in prose (alias ledger); the opponent
#    seater (#4) seats it BY THE PROSE NAME. Real production functions, one
#    scene, one identity — and the GM-panel lie-detector span fires.
# ---------------------------------------------------------------------------


class TestAllSourcesOneSceneOneIdentity:
    def test_mm_seeded_creature_survives_prose_rename_and_reseat(
        self, monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
    ) -> None:
        # Source #1 — MM injection materializes the bound bestiary creature.
        snap = _inject_room_creature(
            monkeypatch,
            patches=[
                NpcPatch(
                    name="Thief",
                    creature_id="thief",
                    threat_level=1,
                    hp=24,
                    manual_origin=True,
                )
            ],
        )
        assert [n.core.name for n in snap.npcs] == ["Thief"], (
            f"MM injection did not materialize exactly one creature: "
            f"{[n.core.name for n in snap.npcs]!r}"
        )
        thief = snap.npcs[0]
        thief.last_seen_location = snap.character_locations["Kirk"]  # co-locate for seating
        assert identity_key(derive_origin(thief), thief.core.name) == "creature:thief"

        # Source #2/#3 (narrator prose) — a rename is recorded as an ALIAS, not a
        # new identity. identity_key is bit-identical before and after.
        accrete_npc_aliases(thief, ["Molgrath the Eyeless"], turn=5)
        assert "Molgrath the Eyeless" in thief.aliases
        assert identity_key(derive_origin(thief), thief.core.name) == "creature:thief"

        # The prose name now resolves to the same entity through the one shared
        # lookup (leg-2 alias hit) — and emits the identity.resolved lie-detector.
        assert resolve_roster_npc(snap.npcs, "Molgrath the Eyeless") is thief

        # Source #4 — the opponent seater seats the enemy BY THE PROSE NAME.
        # Pre-162-2 this minted an ephemeral HP-10 stub beside Thief (the fork).
        _seed_opponent(snap, "Molgrath the Eyeless")

        assert len(snap.npcs) == 1, (
            f"the scene forked into multiple identities: {[n.core.name for n in snap.npcs]!r}"
        )
        assert not any(n.ephemeral for n in snap.npcs), "a stub was minted beside the real creature"
        assert identity_key(derive_origin(snap.npcs[0]), snap.npcs[0].core.name) == "creature:thief"

        # OTEL: the GM panel can verify the alias binding actually engaged
        # (CLAUDE.md OTEL principle — the lie detector for id resolution).
        resolved = [
            s for s in otel_capture.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN
        ]
        assert resolved, (
            "no identity.resolved span — the alias binding is invisible to the GM panel"
        )
        attrs = dict(resolved[-1].attributes or {})
        assert attrs.get("canonical") == "Thief"
        assert attrs.get("identity_key") == "creature:thief"
        assert attrs.get("via") == "alias"

    def test_mm_injection_is_idempotent_across_turns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injection re-fires every turn pre-narrator (websocket_session_handler
        :835-865). Re-injecting the same creature must NOT double-materialize it
        — the id-keyed dedup makes ADR-139's HP carve-out structural (one
        identity, live state preserved)."""
        patches = [
            NpcPatch(name="Thief", creature_id="thief", threat_level=1, hp=24, manual_origin=True)
        ]
        snap = _inject_room_creature(monkeypatch, patches=patches)
        assert len(snap.npcs) == 1

        # Second turn: same room builder fires again against the populated scene.
        sd = SimpleNamespace(monster_manual=None, genre_pack=None, world_slug="beneath_sunden")
        monster_manual_inject.inject(
            sd,  # type: ignore[arg-type]
            snap,
            current_location="entrance",
            in_combat=False,
            room_id="entrance",
        )
        names = [n.core.name for n in snap.npcs]
        assert names == ["Thief"], f"re-injection double-materialized the creature: {names!r}"


# ---------------------------------------------------------------------------
# 3. Authored source (#5) wiring: preload_authored_npcs stamps AUTHORED origin +
#    authored_id, and a prose epithet accretes as an alias without forking.
# ---------------------------------------------------------------------------


class TestAuthoredSourceIsIdKeyed:
    def _fresh_state(self) -> SimpleNamespace:
        return SimpleNamespace(
            characters=[],  # fresh-session signal (no seated PC yet)
            npcs=[],
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
        )

    def test_preload_authored_is_authored_keyed_and_renames_to_alias(self) -> None:
        state = self._fresh_state()

        preload_authored_npcs(
            state,
            [
                AuthoredNpc(
                    id="molgrath",
                    name="Molgrath the Eyeless",
                    role="assassin",
                    initial_disposition=-40,
                )
            ],
        )

        assert len(state.npcs) == 1, "the authored cast did not materialize"
        npc = state.npcs[0]
        assert identity_key(derive_origin(npc), npc.core.name) == "authored:molgrath", (
            "the authored NPC is not id-keyed — preload must stamp AUTHORED + authored_id"
        )

        # A prose epithet is a display alias, never a second identity.
        accrete_npc_aliases(npc, ["The Eyeless One"], turn=2)
        assert "The Eyeless One" in npc.aliases
        assert identity_key(derive_origin(npc), npc.core.name) == "authored:molgrath"
        assert resolve_roster_npc(state.npcs, "The Eyeless One") is npc

    def test_authored_and_bestiary_of_same_being_do_not_double_seat(self) -> None:
        """A creature authored as a named character (authored id) and also
        present as a bestiary row (creature id) are DIFFERENT identity spaces by
        design (ADR-156 §1: authored outranks; the merge, not a fork, handles
        placement-vs-stats). Guard that the keys are stable and distinct so a
        future Green Room merges them by precedence rather than colliding."""
        authored = identity_key(
            Origin(kind=OriginKind.AUTHORED, authored_id="molgrath"), "Molgrath the Eyeless"
        )
        bestiary = identity_key(Origin(kind=OriginKind.MANUAL_POOL, creature_id="thief"), "Thief")
        assert authored == "authored:molgrath"
        assert bestiary == "creature:thief"
        assert authored != bestiary
