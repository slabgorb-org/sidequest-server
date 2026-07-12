"""Story 162-2 (RED) — the seams stop forking identity: seat, record, dedup by id.

The beneath_sunden case file (survey §5): the router names the adversary as a
free string ("Hold-Dead, Still at the Shift"); the seater's ``by_name`` dict is
EXACT-match over ``snapshot.npcs``; the narrator's prose rename ("Molgrath the
Eyeless" over creature_id ``Thief``) matches nothing, so the seater mints an
ephemeral HP-10 stub NEXT TO the bound creature — two names, one enemy, two
entities. 108-2 taught ``_resolve_opponent_from_roster`` to conscript the bound
creature, but the prose name is DROPPED after seating: nothing records that
"Hold-Dead, Still at the Shift" IS Molgrath, so every later reference re-runs
the whole guessing stack.

WHAT THIS SUITE PINS (AC2 seating + AC3 ledger + AC4 fork-kill + AC5 spans),
driving the REAL production seams (no source-text assertions):

1. ``_seed_combat_hp_depletion_to_npcs`` finds an opponent through the unified
   resolver (``resolve_roster_npc``: canonical + aliases + invented_from, one
   normalization) — an alias-named actor seats the canonical entity; no stub.
2. (RETIRED by ADR-156 Amendment A / 166-5) ``_resolve_opponent_from_roster``
   conscripted a bound creature for a router-named free string and recorded
   the prose name as its alias. The conscription itself is deleted: a
   router-named free string with no roster match now seats AS GIVEN, never
   substituted for a co-located creature — see
   tests/server/test_166_5_wrong_other_repros.py.
3. (RETIRED by 162-3) A genuinely-novel opponent no longer mints a stub on the
   default path — the authored bestiary ``generics:`` section is the sanctioned
   last resort and fabrication fails loud. Successor contract (including the
   EPHEMERAL_STUB stamp on the explicit degenerate opt-in) lives in
   tests/server/test_162_3_generics_last_resort_seating.py.
4. ``inject``'s authored-vs-procedural dedup keys on ``identity_key`` (the
   creature_id), not the display name — name drift between a room binding and
   the frozen roster no longer double-materializes the same creature.

RED today: no ``origin`` module / ``Npc.origin`` field; the seater matches
exact names only; conscription drops the router name; inject dedups by name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterActor
from sidequest.game.origin import resolve_roster_npc
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc, NpcPatch
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, ResolutionMode
from sidequest.server.dispatch import monster_manual_inject
from sidequest.server.dispatch.encounter_lifecycle import (
    _seed_combat_hp_depletion_to_npcs,
    instantiate_encounter_from_trigger,
)

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"
_LOC = "the_dropmouth"


def _statted_creature(
    name: str,
    *,
    creature_id: str = "Thief",
    hp: int = 24,
    aliases: list[str] | None = None,
    location: str | None = _LOC,
    disposition: int = -20,
) -> Npc:
    """A bound bestiary creature: ``creature_id`` set, real HP pool, hostile."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="A dead delver still at the shift.",
            personality="Relentless.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
            armor_class=15,
        ),
        creature_id=creature_id,
        threat_level=1,
        disposition=disposition,
        aliases=aliases or [],
        last_seen_location=location,
        last_seen_turn=4,
    )


def _snapshot_with(npc: Npc | None, *, player: str = "Kirk") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[player] = _LOC
    if npc is not None:
        snap.npcs.append(npc)
    return snap


def _combat_cdef() -> ConfrontationDef:
    """Minimal hp_depletion combat cdef (the toothless-detector fixture shape)."""
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
        opponent_default_stats={
            "Strength": 10,
            "hp": 8,
            "armor_class": 12,
            "dexterity": 11,
        },
        opponent_damage=DamageSpec(dice="1d6"),
        beats=[strike],
    )


def _seed(snap: GameSnapshot, opponent_name: str) -> None:
    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=[EncounterActor(name=opponent_name, role="combatant", side="opponent")],
        cdef=_combat_cdef(),
        turn=5,
        source="encounter_handshake",
        acting_character_name="Kirk",
        ruleset=get_ruleset_module("wwn"),
    )


# ---------------------------------------------------------------------------
# 1. The seeder seats through the unified resolver (AC2)
# ---------------------------------------------------------------------------


class TestSeederSeatsByAlias:
    def test_alias_named_actor_seats_canonical_creature_no_stub(self) -> None:
        """The actor arrives named by the prose alias. Today the exact-match
        ``by_name`` dict misses and a stub is minted beside the real creature
        — the literal two-names-one-enemy fork. The resolver leg kills it."""
        ghast = _statted_creature("Vellum Ghast", creature_id="ghast", aliases=["The Pale King"])
        snap = _snapshot_with(ghast)

        _seed(snap, "The Pale King")

        assert len(snap.npcs) == 1, (
            f"expected the alias to seat the canonical creature; roster grew to "
            f"{[n.core.name for n in snap.npcs]!r}"
        )
        assert not any(n.ephemeral for n in snap.npcs), "a stub was minted beside it"

    def test_alias_seated_creature_keeps_its_own_statted_hp(self) -> None:
        """The 108-2 rule extends through the alias leg: the bound creature's
        WWN-balanced HP (24) survives; the cdef's generic default (8) must not
        clobber it (SOUL "Bind the Ruleset, Don't Balance It")."""
        ghast = _statted_creature(
            "Vellum Ghast", creature_id="ghast", hp=24, aliases=["The Pale King"]
        )
        snap = _snapshot_with(ghast)

        _seed(snap, "The Pale King")

        # Guard the scenario first: RED today because the alias leg doesn't
        # exist and a stub is minted (ghast untouched would pass vacuously).
        assert len(snap.npcs) == 1, (
            f"alias did not seat the canonical creature: {[n.core.name for n in snap.npcs]!r}"
        )
        assert ghast.core.hp.max == 24
        assert ghast.core.hp.current == 24
        assert ghast.core.armor_class == 15

    def test_casefold_variant_actor_does_not_mint_a_twin(self) -> None:
        """One normalization at the seam: 'vellum ghast' is 'Vellum Ghast'.
        Today the exact-match dict forks on case alone."""
        snap = _snapshot_with(_statted_creature("Vellum Ghast", creature_id="ghast"))

        _seed(snap, "vellum ghast")

        assert len(snap.npcs) == 1, (
            f"case variance minted a twin: {[n.core.name for n in snap.npcs]!r}"
        )


# ---------------------------------------------------------------------------
# 2. RETIRED by ADR-156 Amendment A (166-5, task 4) — the 108-2 conscription
# this class pinned (``_resolve_opponent_from_roster`` seating a co-located
# bound creature in a router-invented free string's place, then recording
# that string as the creature's alias) is deleted outright: Amendment A's
# target-first seater seats the router's named string AS GIVEN when it is
# not a roster match — it never substitutes a different creature, so there
# is no conscription event left to record an alias for. See
# tests/server/test_166_5_wrong_other_repros.py for the replacement
# contract (named target wins; the resolver leg below still canonicalizes
# a genuine alias/case-variant hit).
# ---------------------------------------------------------------------------
# 3. RETIRED by story 162-3 — the genuinely-novel case no longer mints.
#
# ``TestNovelStubStampsOrigin`` pinned the 108-2 fabrication as the correct
# last resort (ephemeral + EPHEMERAL_STUB stamp + minted-stub span). 162-3
# replaces that last resort with the authored bestiary ``generics:`` section
# and turns default-path fabrication into a loud failure. The successor
# contract — generics seat, loud refusal, and the EPHEMERAL_STUB stamp
# surviving on the explicit degenerate opt-in — is pinned in
# tests/server/test_162_3_generics_last_resort_seating.py.
# ---------------------------------------------------------------------------
# 4. inject dedups authored-vs-procedural by identity_key, not display name
# ---------------------------------------------------------------------------


class TestInjectDedupsByIdentityKey:
    def _run_inject(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        authored: list[NpcPatch],
        region_pop: list[NpcPatch],
    ) -> GameSnapshot:
        """Drive the REAL ``inject`` dedup seam with the two room-path builders
        stubbed at their module seams (their content plumbing is 107-2/153-x
        covered elsewhere; THIS test owns only the dedup decision between
        their outputs)."""
        snap = GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            turn_manager=TurnManager(interaction=3),
        )
        snap.character_locations["Kirk"] = "entrance"
        sd = SimpleNamespace(monster_manual=None, genre_pack=None, world_slug="beneath_sunden")
        monkeypatch.setattr(
            monster_manual_inject,
            "_npc_patches_for_room_binding",
            lambda *a, **k: list(authored),
        )
        monkeypatch.setattr(
            monster_manual_inject,
            "_npc_patches_for_region_population",
            lambda *a, **k: list(region_pop),
        )
        monster_manual_inject.inject(
            sd,  # type: ignore[arg-type]
            snap,
            current_location="entrance",
            in_combat=False,
            room_id="entrance",
        )
        return snap

    def test_name_drifted_procedural_counterpart_dedups_on_creature_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The room binding materializes the bestiary 'Gnaw-Swarm'; the frozen
        procedural roster carries the SAME creature ('gnaw_swarm') under a
        drifted display name. Name-keyed dedup misses → the same creature
        materializes twice (§4 conflict #1). Id-keyed dedup: ONE entity, the
        authored name wins (authored content dominates)."""
        snap = self._run_inject(
            monkeypatch,
            authored=[
                NpcPatch(
                    name="Gnaw-Swarm",
                    creature_id="gnaw_swarm",
                    threat_level=1,
                    hp=6,
                    manual_origin=True,
                )
            ],
            region_pop=[
                NpcPatch(
                    name="Gnaw Swarm",  # roster drift: hyphen lost
                    creature_id="gnaw_swarm",
                    threat_level=1,
                    hp=6,
                    region="entrance",
                    manual_origin=True,
                )
            ],
        )
        names = [n.core.name for n in snap.npcs]
        assert names == ["Gnaw-Swarm"], (
            f"same creature_id materialized twice under drifted names: {names!r}"
        )

    def test_distinct_creature_ids_both_materialize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paranoia negative: id-keying must not over-merge — a DIFFERENT
        procedural creature still lands beside the authored one."""
        snap = self._run_inject(
            monkeypatch,
            authored=[
                NpcPatch(
                    name="Gnaw-Swarm",
                    creature_id="gnaw_swarm",
                    threat_level=1,
                    hp=6,
                    manual_origin=True,
                )
            ],
            region_pop=[
                NpcPatch(
                    name="Pale Lurker",
                    creature_id="pale_lurker",
                    threat_level=2,
                    hp=9,
                    region="entrance",
                    manual_origin=True,
                )
            ],
        )
        assert sorted(n.core.name for n in snap.npcs) == ["Gnaw-Swarm", "Pale Lurker"]


# ---------------------------------------------------------------------------
# Rework round 1 (review [HIGH]): seat-name canonicalization — an alias- or
# case-variant-resolved seat must reference the CANONICAL core name, or the
# opponent is unreachable downstream (find_creature_core is exact-match:
# HP-bar filter at websocket_session_handler.py:202 drops it, WN attack
# returns not_found). Pre-162-2 the seeder GUARANTEED reachability (exact hit
# or stub named exactly actor.name) — its own docstring states the invariant.
# ---------------------------------------------------------------------------


class TestSeatNameCanonicalization:
    def test_alias_seat_canonicalizes_actor_name_and_core_is_reachable(self) -> None:
        """RED (rework): the seeder resolves the ghast but leaves the seat
        named by the alias — actor.name must be rewritten to the canonical
        core name (mirroring 108-2 conscription) so every downstream
        ``find_creature_core(actor.name)`` consumer can reach the opponent.
        The alias itself stays in the ledger for prose."""
        ghast = _statted_creature("Vellum Ghast", creature_id="ghast", aliases=["The Pale King"])
        snap = _snapshot_with(ghast)
        # 162-10 decoy-roster hardening (162-2 finding L355): a co-located decoy
        # with a DISTINCT creature_id, seated FIRST, so a "canonicalize to
        # roster[0]" mutation would rename the seat to the decoy and this test
        # catches it — a single-entry roster cannot tell "resolver match" from
        # "whatever is in the roster".
        decoy = _statted_creature("Bone Piper", creature_id="piper")
        snap.npcs.insert(0, decoy)
        actor = EncounterActor(name="The Pale King", role="combatant", side="opponent")

        _seed_combat_hp_depletion_to_npcs(
            snapshot=snap,
            actors=[actor],
            cdef=_combat_cdef(),
            turn=5,
            source="encounter_handshake",
            acting_character_name="Kirk",
            ruleset=get_ruleset_module("wwn"),
        )

        assert actor.name == "Vellum Ghast", (
            f"seat kept the alias {actor.name!r} — unreachable by every "
            f"find_creature_core consumer (HP bars, WN attack, query_encounter)"
        )
        assert resolve_roster_npc(snap.npcs, actor.name) is ghast, (
            "the seat must canonicalize to the SPECIFIC resolver-matched creature, "
            "not merely to some roster member (the decoy)"
        )
        core = snap.find_creature_core(actor.name)
        assert core is not None, "seated opponent core unreachable by seat name"
        # The exact predicate the HP-bar overlay filter applies
        # (websocket_session_handler.py:202) — the opponent must survive it.
        assert core.hp.max > 0

    def test_case_variant_seat_canonicalizes_actor_name(self) -> None:
        """RED (rework): same invariant through the normalization leg —
        a case/whitespace-variant seat name is rewritten to canonical."""
        ghast = _statted_creature("Vellum Ghast", creature_id="ghast")
        snap = _snapshot_with(ghast)
        # 162-10 decoy-roster hardening (162-2 finding L355): decoy seated first.
        snap.npcs.insert(0, _statted_creature("Bone Piper", creature_id="piper"))
        actor = EncounterActor(name="vellum ghast", role="combatant", side="opponent")

        _seed_combat_hp_depletion_to_npcs(
            snapshot=snap,
            actors=[actor],
            cdef=_combat_cdef(),
            turn=5,
            source="encounter_handshake",
            acting_character_name="Kirk",
            ruleset=get_ruleset_module("wwn"),
        )

        assert actor.name == "Vellum Ghast"
        assert resolve_roster_npc(snap.npcs, actor.name) is ghast, (
            "the case-variant seat must canonicalize to the SPECIFIC matched creature"
        )
        assert snap.find_creature_core(actor.name) is not None

    def test_instantiate_with_alias_threat_seats_canonical_actor_name(self) -> None:
        """RED (rework): the head-check path. The router names a RECORDED
        alias of a co-located bound creature. Today `_resolve_opponent_from_
        roster` early-returns None on the alias hit ("seat directly"), so the
        encounter actor carries the alias — REGRESSING the pre-162-2 flow
        where conscription seated this creature canonically. Post-fix the
        seated opponent must be the canonical name, core reachable."""
        ghast = _statted_creature("Vellum Ghast", creature_id="ghast", aliases=["The Pale King"])
        snap = _snapshot_with(ghast)
        # 162-10 decoy-roster hardening (162-2 finding L355): decoy seated first so
        # a "seat whatever's in the roster" mutation would rename the opponent to
        # the decoy — a single-entry roster could not distinguish the two.
        decoy = _statted_creature("Bone Piper", creature_id="piper")
        snap.npcs.insert(0, decoy)

        enc = instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=load_genre_pack(_FIXTURE_PACK),
            encounter_type="combat",
            player_name="Kirk",
            npcs_present=[],
            genre_slug=snap.genre_slug,
            materialized_threat=NpcMention(name="The Pale King", role="hostile", side="opponent"),
        )

        opponents = [a.name for a in enc.actors if a.side == "opponent"]
        assert opponents == ["Vellum Ghast"], (
            f"alias threat seated under its alias {opponents!r} — encounter "
            f"actor unresolvable by find_creature_core"
        )
        # No twin minted: the roster stays [decoy, ghast] (2), and the opponent
        # resolves to the SPECIFIC ghast, not the decoy or a fabricated stub.
        assert len(snap.npcs) == 2, f"a twin was minted: {[n.core.name for n in snap.npcs]!r}"
        assert not any(n.ephemeral for n in snap.npcs)
        for a in enc.actors:
            if a.side == "opponent":
                assert resolve_roster_npc(snap.npcs, a.name) is ghast
                assert snap.find_creature_core(a.name) is not None


# ---------------------------------------------------------------------------
# Rework round 1 (review [MEDIUM]): the id-leg dedup guard, ISOLATED — the
# integration negative above can pass on the name leg alone (review finding:
# its display names also differ). These unit-pin _patch_identity_key itself:
# same display name + different creature ids MUST key apart (only the id leg
# can produce that), and drifted names + same id MUST key together.
# CONTRACT GUARDS: green on arrival by design — they pin the mechanism the
# integration test cannot isolate; they must stay green.
# ---------------------------------------------------------------------------


class TestPatchIdentityKeyUnit:
    def test_same_display_name_different_creature_ids_key_apart(self) -> None:
        from sidequest.server.dispatch.monster_manual_inject import _patch_identity_key

        a = NpcPatch(name="Gnaw-Swarm", creature_id="gnaw_swarm", threat_level=1, hp=6)
        b = NpcPatch(name="Gnaw-Swarm", creature_id="swarm", threat_level=1, hp=6)
        assert _patch_identity_key(a) != _patch_identity_key(b), (
            "distinct bestiary ids under one display name must not collapse — "
            "the id leg is the ONLY thing separating them"
        )

    def test_drifted_display_names_same_creature_id_share_key(self) -> None:
        from sidequest.server.dispatch.monster_manual_inject import _patch_identity_key

        a = NpcPatch(name="Gnaw-Swarm", creature_id="gnaw_swarm", threat_level=1, hp=6)
        b = NpcPatch(name="Gnaw Swarm", creature_id="gnaw_swarm", threat_level=1, hp=6)
        assert _patch_identity_key(a) == _patch_identity_key(b)
