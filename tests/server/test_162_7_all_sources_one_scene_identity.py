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

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.alias_accretion import accrete_npc_aliases
from sidequest.game.encounter import EncounterActor
from sidequest.game.monster_manual import EntryState, ManualEncounter, ManualNpc, MonsterManual
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
from sidequest.genre.models.bestiary import BestiaryEntry
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, ResolutionMode, RulesConfig
from sidequest.server.dispatch import monster_manual_inject
from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger
from sidequest.server.narration_apply import _apply_npc_mentions
from sidequest.server.session_helpers import _auto_mint_prose_only_npcs

_IDENTITY_RESOLVED_SPAN = "identity.resolved"
_MATERIALIZED_SPAN = "green_room.materialized"
_MINT_SPAN = "green_room.mint"


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


# ---------------------------------------------------------------------------
# 4. Task 6 (Green Room implementation plan) — the all-sources-one-scene
#    extension. Every feeder family that can genuinely co-occur in ONE
#    staged scene, driven through real production functions end to end.
#
# LABEL-DRIFT NOTE (read before touching this section): the implementation
# plan's Task 2 Interfaces list named eight ``canonical_source`` labels for
# this guard, including "zone_cast" and "router_opponent" as their own
# distinct labels. Neither exists in the landed code (user-adjudicated,
# 2026-07-11/-12):
#
#   * "zone_cast" never happened as its own append site — zone-cast staging
#     does not append directly to ``snapshot.npcs``; its cast reaches the
#     stage through the other feeders (MM room-binding / region-population),
#     so there is no separate "zone_cast" ``canonical_source``.
#   * "router_opponent" is not a bespoke label either. A router-named novel
#     person with no roster/pool backing seats through
#     ``encounter_lifecycle``'s existing last-resort branch, which stamps
#     ``canonical_source="seeder.generics"`` (Task 4's two-outcome contract:
#     a router name resolves to the roster, the pool, OR the world's
#     authored bestiary generics — never a bespoke "router_opponent" tier).
#     ``seeder.generics`` therefore IS the router-opponent feeder Amendment A
#     describes; it is asserted below under its real label.
#
# The full ``canonical_source`` taxonomy that actually exists in production
# today (grep -rn "canonical_source\|source=\"" sidequest/ — the four call
# sites this file's own imports touch, plus the two this suite delegates):
#
#   * Task 2 (``monster_manual_inject.py``): "mm.available_humans",
#     "mm.encounters", "mm.room_binding", "mm.region_population"
#   * Task 3 (``world_materialization.py``): "preload_authored" (session-start
#     authored cast), "worldbuilder_history" (history-chapter-authored NPC)
#   * Task 3 (``narration_apply.py`` promotions): "pool_promotion"
#   * Task 3 (``encounter_lifecycle.py`` native seeder): "seeder.pool_promotion",
#     "seeder.generics"
#   * Task 3 (``encounter_lifecycle.py`` Fate seeder): "fate_seeder.pool_promotion",
#     "fate_seeder.frame"
#
# This suite stages the SIX feeders whose preconditions do not contradict
# each other in one scene: the four MM feeders (fire together from one
# ``monster_manual_inject.inject()`` call), "preload_authored" (its only
# gate is "no PC seated yet" — ``state.characters == []`` — which an
# MM-populated ``snapshot.npcs`` does not violate), and "seeder.generics"
# (driven by naming a router opponent with no roster/pool backing — the
# router-opponent feeder). The other four materialized-tier labels are
# deliberately NOT forced into this scene and are delegated to their own
# pinning tests instead:
#
#   * "worldbuilder_history" needs a history-chapter apply pass
#     (``WorldMaterializer._apply_npc``) — orthogonal setup already covered
#     by ``tests/server/test_green_room_append_sites.py``.
#   * "pool_promotion" needs an ALREADY-pool-staged member promoted on a
#     LATER turn — covered by ``test_green_room_append_sites.py`` and
#     exercised end-to-end by ``test_green_room_attach_before_mint.py``. This
#     scene instead drives the mention-mint feeders (Task 5) that PRODUCE the
#     pool members a later promotion would consume, without promoting them
#     (promotion is a separate turn in a real session).
#   * "fate_seeder.pool_promotion" / "fate_seeder.frame" need a Fate-bound
#     pack (``pack.rules.ruleset == "fate"``) — a different ruleset binding
#     than this scene's default ("dial"), covered by the Fate-specific
#     dispatch suites under ``tests/server/dispatch/``.
#
# The mention-path feeders (Task 5: narrator mentions, prose extraction) do
# NOT emit ``green_room.materialized`` at all — per ADR-156 §6 they mint POOL
# members (``green_room.mint``) and attach aliases (``green_room.alias_
# attached``), reaching ``green_room.materialized`` only at pool promotion.
# The companion test below stages both mint sources in the SAME scene and
# asserts the mint evidence directly, plus that pool-tier staging never
# perturbs the materialized-layer identity count from the first test.
# ---------------------------------------------------------------------------

_ROUTER_OPPONENT_NAME = "Corvid Ashgrave"


def _human_manual_npc(name: str) -> ManualNpc:
    return ManualNpc(
        data={"name": name, "role": "wanderer", "culture": "Sunden", "ocean_summary": "watchful"},
        name=name,
        role="wanderer",
        culture="Sunden",
        state=EntryState.AVAILABLE,
    )


def _encounter_manual_entry(enemy_name: str, *, hp: int = 6) -> ManualEncounter:
    return ManualEncounter(
        data={
            "enemies": [
                {"name": enemy_name, "class": "salt_burrower", "hp": hp, "role": "ambusher"}
            ]
        },
        label=f"1x {enemy_name} (tier 1)",
        tier=1,
        state=EntryState.AVAILABLE,
    )


class _RouterOpponentPack:
    """Duck-typed pack for the ``seeder.generics`` feeder — same combat cdef
    + generics-carrying bestiary shape as
    ``test_162_3_generics_last_resort_seating.py::_FakeGenrePack`` and
    ``test_166_5_wrong_other_repros.py::_FakeGenrePack``."""

    def __init__(self) -> None:
        self.rules = RulesConfig(confrontations=[_combat_cdef()])
        self._bestiary = SimpleNamespace(
            entries=[],
            generics=[
                BestiaryEntry(
                    id="drifter",
                    name="A Drifter",
                    level=1,
                    hp=8,
                    armor_class=11,
                    attack_bonus=1,
                    damage="1d6",
                    role="generic Other",
                )
            ],
        )

    def effective_bestiary(self, world: str | None) -> tuple[object | None, str]:
        return self._bestiary, "world"


@pytest.fixture
def staged_scene(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Stage the six co-occurring feeders in one scene: MM inject fires all
    four of its feeders in a single ``inject()`` call (mirrors
    ``test_green_room_mm_feeders.py::mm_session_fixture``); the fresh-session
    authored preload runs next (its gate never conflicts with an
    MM-populated roster); then a router-named opponent with no roster/pool
    backing seats via the world's authored bestiary generics — the
    router-opponent feeder (Amendment A), landing under
    ``canonical_source="seeder.generics"``."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Kirk"] = "Salt Camp"

    manual = MonsterManual(
        genre="caverns_and_claudes",
        world="beneath_sunden",
        npcs=[_human_manual_npc("Pregen Wanderer")],
        encounters=[_encounter_manual_entry("Salt Burrower", hp=6)],
    )
    sd = SimpleNamespace(
        monster_manual=manual,
        genre_pack=None,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    monkeypatch.setattr(
        monster_manual_inject,
        "_npc_patches_for_room_binding",
        lambda *a, **k: [
            NpcPatch(
                name="Gnaw-Swarm",
                creature_id="gnaw_swarm",
                threat_level=1,
                hp=10,
                manual_origin=True,
                origin=Origin(kind=OriginKind.ROOM_BOUND, creature_id="gnaw_swarm"),
            )
        ],
    )
    monkeypatch.setattr(
        monster_manual_inject,
        "_npc_patches_for_region_population",
        lambda *a, **k: [
            NpcPatch(
                name="Pale Lurker",
                creature_id="pale_lurker",
                threat_level=2,
                hp=9,
                region="toods_dome",
                manual_origin=True,
                origin=Origin(kind=OriginKind.REGION_POPULATION, creature_id="pale_lurker"),
            )
        ],
    )
    monster_manual_inject.inject(
        sd,  # type: ignore[arg-type]
        snap,
        current_location="Salt Camp",
        in_combat=False,
        room_id="toods_dome",
    )

    # Feeder 5 — preload_authored (world_materialization, Task 3): the
    # fresh-session authored cast. Gate is "no PC seated yet"
    # (state.characters == []), which still holds here — MM inject only
    # touches snap.npcs, never snap.characters.
    preload_authored_npcs(
        snap,
        [
            AuthoredNpc(
                id="camp_elder", name="Doña Yarrow", role="camp elder", initial_disposition=10
            )
        ],
    )

    # Feeder 6 — seeder.generics (encounter_lifecycle, Task 3 / Amendment A):
    # the router names a person with NO roster/pool backing; the world's
    # authored bestiary generics seat them as the confrontation Other.
    pack = _RouterOpponentPack()
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]  # duck-typed; see _RouterOpponentPack
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[NpcMention(name=_ROUTER_OPPONENT_NAME, role="hostile", side="opponent")],
        genre_slug=snap.genre_slug,
    )

    return SimpleNamespace(snapshot=snap, otel=otel_capture, encounter=enc)


class TestSixFeederOneSceneWiring:
    """Task 6: the all-sources-one-scene extension, at the label set that
    actually exists (see the module-level LABEL-DRIFT NOTE above)."""

    def test_six_feeders_materialize_one_identity_each_and_router_opponent_seats(
        self, staged_scene: SimpleNamespace
    ) -> None:
        snap = staged_scene.snapshot
        enc = staged_scene.encounter

        # (a) exactly one identity per staged creature, keyed by identity_key
        # — the materialized-layer invariant this suite's SCOPE note (top of
        # file) describes, now driven across every feeder that can co-occur.
        keys = [identity_key(derive_origin(n), n.core.name) for n in snap.npcs]
        assert len(snap.npcs) == len(set(keys)) == 6, (
            f"expected 6 distinct materialized identities (4 MM feeders + "
            f"preload_authored + seeder.generics); got {len(snap.npcs)} npcs, "
            f"{len(set(keys))} distinct keys: {keys!r}"
        )

        # (b) a green_room.materialized span per feeder source label that can
        # genuinely land in this scene (six of the real taxonomy's labels —
        # see the LABEL-DRIFT NOTE for why the other four are delegated).
        materialized = [
            s for s in staged_scene.otel.get_finished_spans() if s.name == _MATERIALIZED_SPAN
        ]
        sources = {dict(s.attributes or {}).get("canonical_source") for s in materialized}
        expected = {
            "mm.available_humans",
            "mm.encounters",
            "mm.room_binding",
            "mm.region_population",
            "preload_authored",
            "seeder.generics",
        }
        assert expected <= sources, (
            f"missing feeder source labels in materialized spans: "
            f"{expected - sources!r}; saw {sources!r}"
        )

        # (c) the named novel person is seated as the confrontation Other —
        # the router-opponent feeder (Amendment A), under its real label.
        assert enc is not None, "the router-named opponent must still seat the encounter"
        opponent = next(a for a in enc.actors if a.side == "opponent")
        assert opponent.name == _ROUTER_OPPONENT_NAME, (
            f"the router-named person must be seated as the Other verbatim; got {opponent.name!r}"
        )
        seated = next(n for n in snap.npcs if n.core.name == _ROUTER_OPPONENT_NAME)
        assert seated.origin is not None and seated.origin.kind == OriginKind.GENERIC, (
            f"the router opponent must seat from the world's authored generics "
            f"(GENERIC origin); got {seated.origin!r}"
        )

    def test_mention_path_mint_evidence_present_in_same_scene(
        self, staged_scene: SimpleNamespace
    ) -> None:
        """Mention feeders (Task 5) reach the SAME staged scene as a later
        turn's addition: they mint POOL members (``green_room.mint``), never
        touching ``snapshot.npcs`` directly — pool staging only reaches
        ``green_room.materialized`` at pool promotion (delegated to
        ``test_green_room_append_sites.py``, per the module-level note). This
        drives both Task-5 mint sources — narrator mention and prose
        extraction — and confirms the materialized-layer identity count from
        the sibling test is undisturbed by pool-tier staging."""
        snap = staged_scene.snapshot
        materialized_npc_count_before = len(snap.npcs)

        # Narrator-mention mint (narration_apply._apply_npc_mentions). Stance
        # is deliberately non-hostile: a hostile stance would open the
        # attach-before-mint seated-Other ("Ihnsch") leg against the scene's
        # live seeder.generics opponent instead of minting fresh — this test
        # wants the plain mint path, not that attach leg (pinned separately
        # in test_green_room_attach_before_mint.py).
        _apply_npc_mentions(
            snapshot=snap,
            mentions=[NpcMention(name="Salt Camp Loremonger", stance="bystander")],
            turn_num=6,
        )

        # Prose-extraction mint (session_helpers._auto_mint_prose_only_npcs)
        # — same narration text as the known-green
        # test_green_room_mint_span_fires_on_genuine_prose_extraction_mint,
        # reused here so the bare-role detection is proven, not guessed.
        _auto_mint_prose_only_npcs(
            snapshot=snap,
            narration_text="Father lies pale. He cannot speak.",
            emitted_mentions=[],
            turn_num=6,
        )

        mint_spans = [
            dict(s.attributes or {})
            for s in staged_scene.otel.get_finished_spans()
            if s.name == _MINT_SPAN
        ]
        mint_sources = {s.get("source") for s in mint_spans}
        assert {"narrator_mention", "prose_extraction"} <= mint_sources, (
            f"expected both mention-path mint sources to fire green_room.mint; "
            f"got sources={mint_sources!r} from spans={mint_spans!r}"
        )
        assert all(s.get("identity_key") for s in mint_spans), (
            f"identity_key must be non-empty on every green_room.mint span; got {mint_spans!r}"
        )
        assert len(snap.npcs) == materialized_npc_count_before, (
            "a mention-path mint must stage into snapshot.npc_pool, never "
            "append to snapshot.npcs — the materialized-layer identity count "
            "must be unaffected by pool-tier staging"
        )
