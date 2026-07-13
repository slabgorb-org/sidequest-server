"""Story 162-2 (RED) — typed ``Origin`` unifies the four provenance fields.

Survey ``docs/superpowers/specs/2026-07-05-npc-generation-inventory.md`` (§4
conflict #7, §8 D1/D2): identity is a name everywhere — MM dedup, purge
membership, seater matching, pool promotion all key on ``name`` strings, each
with a different normalization. Provenance is smeared across four partial
fields (``invented_from`` / ``pool_origin`` / ``manual_origin`` /
``creature_id``) with no typed arbiter input. The narrator's prose rename
("Molgrath the Eyeless" over creature_id ``Thief``) forks identity.

THE CONTRACT THIS SUITE PINS (the test IS the spec) — reuse-first per D1
("unify, don't add a new system"): the legacy fields stay for round-trip;
``Origin`` is the single typed view over them, stamped going forward.

    # sidequest.game.origin  (NET-NEW module)

    class OriginKind(str, Enum):
        AUTHORED = "authored"                      # npcs.yaml cast (world_materialization)
        ROOM_BOUND = "room_bound"                  # room YAML encounter_creatures (107-2)
        REGION_POPULATION = "region_population"    # ADR-106 frozen procedural roster
        MANUAL_POOL = "manual_pool"                # ADR-059 MM pregen (namegen/encountergen)
        NARRATOR_INVENTED = "narrator_invented"    # narrator mention / prose mint
        EPHEMERAL_STUB = "ephemeral_stub"          # seater fabrication (108-2)

    class Origin(BaseModel):        # extra="forbid"
        kind: OriginKind            # REQUIRED — no silent default kind
        creature_id: str | None = None    # bestiary id, when creature-derived
        authored_id: str | None = None    # AuthoredNpc.id, when world-authored
        invented_from: str | None = None  # narrator's original invented name
        content_version: str | None = None  # content sha (162-1 reconcile keying)

    def normalize_name(name: str) -> str
        # THE single name normalization for every identity seam: casefold,
        # strip, collapse internal whitespace. Kills the per-seam divergence
        # (exact / casefold / comma-inverted) called out in §2 "Friction".

    def identity_key(origin: Origin | None, display_name: str) -> str
        # The dedup/purge/seating key (AC2). Precedence: authored_id, then
        # creature_id, then normalized display name. ``origin=None`` (legacy,
        # unstamped) keys on the normalized name. Tests below assert KEY
        # EQUALITY PROPERTIES, not literal formats — Dev owns the format.

    def derive_origin(npc: Npc) -> Origin
        # A stamped ``npc.origin`` wins verbatim. Otherwise derive from the
        # legacy fields (derive-don't-migrate, the 162-1 pattern):
        #   ephemeral=True                          -> EPHEMERAL_STUB
        #   manual_origin=True and region set       -> REGION_POPULATION
        #   manual_origin=True (no region)          -> MANUAL_POOL
        #   otherwise                               -> NARRATOR_INVENTED
        # ROOM_BOUND and AUTHORED are NOT derivable from legacy fields (a
        # room-bound patch is byte-identical to an encounter patch today) —
        # which is exactly why creation paths must stamp ``origin`` forward.

    # sidequest.game.session
    Npc.origin: Origin | None = None       # None on legacy saves (pre-162-2 JSON)
    NpcPatch.origin: Origin | None = None  # builders stamp; materializer carries;
                                           # merge is monotonic (a later origin-less
                                           # patch never clears a stamped origin)

Wiring pinned here (Verify Wiring, Not Just Existence):
  * ``preload_authored_npcs`` stamps AUTHORED + ``AuthoredNpc.id``
  * ``_creature_patch_from_enemy`` stamps MANUAL_POOL + creature_id
  * ``_creature_patch_from_bestiary_entry`` stamps ROOM_BOUND + creature_id
  * ``_creature_patch_from_region_creature`` stamps REGION_POPULATION + creature_id

RED today: ``sidequest.game.origin`` does not exist; ``Npc`` / ``NpcPatch``
are ``extra="forbid"`` and reject an ``origin`` key. Net-new symbols are
imported INSIDE each test so collection survives and each fails crisply.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, NpcPatch


def _bare_npc(name: str, **kwargs) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        **kwargs,
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[],
    )


# ---------------------------------------------------------------------------
# AC1 — OriginKind: the six production creation families, typed
# ---------------------------------------------------------------------------


class TestOriginKind:
    def test_origin_kind_has_the_six_creation_families(self) -> None:
        """One member per §3a creation family that lands an ``Npc``. The
        narrator-mention and prose-extraction paths share NARRATOR_INVENTED
        (both are narrator mints per §3a paths 2/3)."""
        from sidequest.game.origin import OriginKind

        expected = {
            "authored",
            "room_bound",
            "region_population",
            "manual_pool",
            "narrator_invented",
            "ephemeral_stub",
        }
        assert {k.value for k in OriginKind} >= expected

    def test_origin_kind_is_string_valued(self) -> None:
        """Kinds serialize as plain strings so ``Origin`` rides the snapshot
        JSON blob (model_dump_json) without a custom encoder."""
        from sidequest.game.origin import OriginKind

        assert OriginKind.AUTHORED == "authored"
        assert isinstance(OriginKind.MANUAL_POOL.value, str)


# ---------------------------------------------------------------------------
# AC1 — Origin model shape
# ---------------------------------------------------------------------------


class TestOriginModel:
    def test_origin_requires_kind(self) -> None:
        """No silent default provenance — an Origin with no kind is a
        construction error, not a guessed value (No Silent Fallbacks)."""
        from sidequest.game.origin import Origin

        with pytest.raises(ValidationError):
            Origin()  # type: ignore[call-arg]

    def test_origin_optional_ids_default_none(self) -> None:
        from sidequest.game.origin import Origin, OriginKind

        origin = Origin(kind=OriginKind.NARRATOR_INVENTED)
        assert origin.creature_id is None
        assert origin.authored_id is None
        assert origin.invented_from is None
        assert origin.content_version is None

    def test_origin_rejects_undeclared_fields(self) -> None:
        """``extra="forbid"`` — provenance is a closed, typed surface, not an
        ad-hoc dict a seam can quietly extend."""
        from sidequest.game.origin import Origin

        with pytest.raises(ValidationError):
            Origin.model_validate({"kind": "authored", "vibe": "mysterious"})


# ---------------------------------------------------------------------------
# AC2 — normalize_name: ONE normalization for every seam
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_casefold_strip_and_whitespace_collapse(self) -> None:
        from sidequest.game.origin import normalize_name

        assert normalize_name("  Molgrath   the EYELESS ") == normalize_name("molgrath the eyeless")

    def test_distinct_names_stay_distinct(self) -> None:
        """Normalization is for spelling variance, not fuzzy matching — two
        different creatures must never collapse into one key."""
        from sidequest.game.origin import normalize_name

        assert normalize_name("Gnaw-Swarm") != normalize_name("Chalk Moth")

    def test_blank_name_normalizes_empty(self) -> None:
        from sidequest.game.origin import normalize_name

        assert normalize_name("   ") == ""

    def test_diacritics_fold_to_ascii_base(self) -> None:
        """RED (rework, review [MEDIUM]/[RULE]): ADR-091 culture names carry
        diacritics ("Veyra Solnë" is the codebase's own documented example);
        an ASCII prose reference must normalize to the same key. The shared
        primitive already exists — ``sidequest.foundation.slug_fold.
        fold_to_ascii``, used by alias_resolution.py for this exact bug
        class — normalize_name must compose it, not casefold alone."""
        from sidequest.game.origin import normalize_name

        assert normalize_name("Veyra Solnë") == normalize_name("Veyra Solne")
        assert normalize_name("Café du Monde") == normalize_name("cafe du monde")

    def test_diacritic_fold_preserves_distinctness(self) -> None:
        """Folding is for spelling variance, not fuzzy matching — genuinely
        different names stay apart after the fold."""
        from sidequest.game.origin import normalize_name

        assert normalize_name("Veyra Solnë") != normalize_name("Veyra Talvi")


# ---------------------------------------------------------------------------
# AC2 — identity_key: id-keyed where an id exists, name-keyed only as floor
# (properties asserted, never literal key formats)
# ---------------------------------------------------------------------------


class TestIdentityKey:
    def test_same_creature_id_different_display_names_share_a_key(self) -> None:
        """The two-names-one-enemy core: the bestiary Thief and the narrator's
        'Molgrath the Eyeless' rename are the SAME identity when both carry
        creature_id='thief'."""
        from sidequest.game.origin import Origin, OriginKind, identity_key

        bound = Origin(kind=OriginKind.MANUAL_POOL, creature_id="thief")
        assert identity_key(bound, "Thief") == identity_key(bound, "Molgrath the Eyeless")

    def test_different_creature_ids_never_share_a_key(self) -> None:
        from sidequest.game.origin import Origin, OriginKind, identity_key

        a = Origin(kind=OriginKind.MANUAL_POOL, creature_id="gnaw_swarm")
        b = Origin(kind=OriginKind.MANUAL_POOL, creature_id="chalk_moth")
        assert identity_key(a, "Lurker") != identity_key(b, "Lurker")

    def test_authored_id_dominates_creature_id(self) -> None:
        """An authored NPC that also carries a bestiary binding keys on its
        authored id — authored content dominates (§8 D1 ordering)."""
        from sidequest.game.origin import Origin, OriginKind, identity_key

        both = Origin(kind=OriginKind.AUTHORED, authored_id="prefect_vaskov", creature_id="thief")
        authored_only = Origin(kind=OriginKind.AUTHORED, authored_id="prefect_vaskov")
        creature_only = Origin(kind=OriginKind.MANUAL_POOL, creature_id="thief")
        assert identity_key(both, "Ilara") == identity_key(authored_only, "Ilara")
        assert identity_key(both, "Ilara") != identity_key(creature_only, "Ilara")

    def test_no_ids_keys_on_normalized_display_name(self) -> None:
        from sidequest.game.origin import Origin, OriginKind, identity_key

        invented = Origin(kind=OriginKind.NARRATOR_INVENTED)
        assert identity_key(invented, " Rifenna  Muse ") == identity_key(invented, "rifenna muse")

    def test_no_ids_distinct_names_differ(self) -> None:
        from sidequest.game.origin import Origin, OriginKind, identity_key

        invented = Origin(kind=OriginKind.NARRATOR_INVENTED)
        assert identity_key(invented, "Rifenna Muse") != identity_key(invented, "Varra")

    def test_none_origin_is_the_legacy_name_key(self) -> None:
        """An unstamped legacy Npc keys on its normalized name — same key a
        stamped id-less origin would produce, so legacy and new entities
        dedup against each other."""
        from sidequest.game.origin import Origin, OriginKind, identity_key

        invented = Origin(kind=OriginKind.NARRATOR_INVENTED)
        assert identity_key(None, "Rux") == identity_key(invented, "Rux")


def test_identity_key_generic_kind_keys_by_name() -> None:
    """A generics row is a stat DONOR, not an identity (Amendment B): two
    named persons backed by the same row must not collide on the row id."""
    from sidequest.game.origin import Origin, OriginKind, identity_key

    g = Origin(kind=OriginKind.GENERIC, creature_id="wasteland_scavenger")
    assert identity_key(g, "the Scrapborn") == "name:the scrapborn"
    assert identity_key(g, "the courier") == "name:the courier"


# ---------------------------------------------------------------------------
# AC1 — Npc.origin / NpcPatch.origin storage + round-trip
# ---------------------------------------------------------------------------


class TestNpcOriginField:
    def test_npc_accepts_stamped_origin(self) -> None:
        from sidequest.game.origin import Origin, OriginKind

        npc = _bare_npc(
            "Gnaw-Swarm",
            origin=Origin(kind=OriginKind.ROOM_BOUND, creature_id="gnaw_swarm"),
        )
        assert npc.origin is not None
        assert npc.origin.kind == OriginKind.ROOM_BOUND
        assert npc.origin.creature_id == "gnaw_swarm"

    def test_npc_origin_json_round_trip(self) -> None:
        """Origin rides the GameSnapshot JSON blob (model_dump_json →
        snapshot_json) losslessly — the ADR-115 persistence path."""
        from sidequest.game.origin import Origin, OriginKind

        npc = _bare_npc(
            "Prefect Ilara Vaskov",
            origin=Origin(kind=OriginKind.AUTHORED, authored_id="prefect_vaskov"),
        )
        reloaded = Npc.model_validate_json(npc.model_dump_json())
        assert reloaded.origin is not None
        assert reloaded.origin.kind == OriginKind.AUTHORED
        assert reloaded.origin.authored_id == "prefect_vaskov"

    def test_legacy_npc_json_without_origin_key_loads_as_none(self) -> None:
        """Pre-162-2 saves have no ``origin`` key — they must load (origin=None),
        never migrate, never crash (the derive-don't-migrate contract)."""
        legacy = json.loads(_bare_npc("Old Save Hand").model_dump_json())
        legacy.pop("origin", None)
        reloaded = Npc.model_validate(legacy)
        assert reloaded.origin is None

    def test_npc_patch_accepts_origin(self) -> None:
        from sidequest.game.origin import Origin, OriginKind

        patch = NpcPatch(
            name="Gnaw-Swarm",
            creature_id="gnaw_swarm",
            threat_level=1,
            hp=6,
            origin=Origin(kind=OriginKind.ROOM_BOUND, creature_id="gnaw_swarm"),
        )
        assert patch.origin is not None
        assert patch.origin.kind == OriginKind.ROOM_BOUND


class TestMaterializerCarriesOrigin:
    # The old ``apply_world_patch(npcs_present=...)`` drive was rewritten onto
    # ``_npc_from_patch`` directly, and the ``_merge_npc_patch`` monotonicity
    # test deleted with that method, when the legacy WorldStatePatch lane was
    # removed (Green Room follow-up, 2026-07-11). Merge-time origin
    # preservation is now ``green_room.admit()``'s no-touch semantics on an
    # existing entry — covered by the green-room suites.

    def test_npc_from_patch_carries_patch_origin_onto_new_npc(self) -> None:
        """The materializer builder every creation path shares: a stamped
        patch materializes an Npc carrying the same typed origin."""
        from sidequest.game.origin import Origin, OriginKind

        snap = _snapshot()
        materialized = snap._npc_from_patch(
            NpcPatch(
                name="Gnaw-Swarm",
                creature_id="gnaw_swarm",
                threat_level=1,
                hp=6,
                manual_origin=True,
                origin=Origin(kind=OriginKind.ROOM_BOUND, creature_id="gnaw_swarm"),
            ),
            emit_spawn_span=False,
        )
        assert materialized.origin is not None
        assert materialized.origin.kind == OriginKind.ROOM_BOUND


# ---------------------------------------------------------------------------
# AC1 — derive_origin: stamped wins; legacy fields derive (never migrate)
# ---------------------------------------------------------------------------


class TestDeriveOrigin:
    def test_ephemeral_stub_derives_ephemeral_stub(self) -> None:
        from sidequest.game.origin import OriginKind, derive_origin

        npc = _bare_npc("Arena Opponent", ephemeral=True)
        assert derive_origin(npc).kind == OriginKind.EPHEMERAL_STUB

    def test_region_stamped_manual_creature_derives_region_population(self) -> None:
        """``region`` is set ONLY by the region-population inject (ADR-106) —
        the one legacy combination that identifies the frozen roster."""
        from sidequest.game.origin import OriginKind, derive_origin

        npc = _bare_npc(
            "Pale Lurker",
            manual_origin=True,
            creature_id="pale_lurker",
            region="room_7",
        )
        derived = derive_origin(npc)
        assert derived.kind == OriginKind.REGION_POPULATION
        assert derived.creature_id == "pale_lurker"

    def test_manual_origin_without_region_derives_manual_pool(self) -> None:
        from sidequest.game.origin import OriginKind, derive_origin

        npc = _bare_npc("Chalk Moth", manual_origin=True, creature_id="chalk_moth")
        derived = derive_origin(npc)
        assert derived.kind == OriginKind.MANUAL_POOL
        assert derived.creature_id == "chalk_moth"

    def test_invented_npc_derives_narrator_invented_and_carries_binding(self) -> None:
        """The perseus double-mint breadcrumb (``invented_from="Varra"``) rides
        into the typed origin so the original→mint binding is part of identity."""
        from sidequest.game.origin import OriginKind, derive_origin

        npc = _bare_npc("Rifenna Muse", invented_from="Varra", pool_origin="Rifenna Muse")
        derived = derive_origin(npc)
        assert derived.kind == OriginKind.NARRATOR_INVENTED
        assert derived.invented_from == "Varra"

    def test_bare_narrator_npc_derives_narrator_invented(self) -> None:
        from sidequest.game.origin import OriginKind, derive_origin

        assert derive_origin(_bare_npc("Shopkeeper")).kind == OriginKind.NARRATOR_INVENTED

    def test_stamped_origin_wins_over_legacy_derivation(self) -> None:
        """A stamped AUTHORED origin outranks legacy fields that would derive
        MANUAL_POOL — the stamp is ground truth, derivation is the fallback."""
        from sidequest.game.origin import Origin, OriginKind, derive_origin

        npc = _bare_npc(
            "Prefect Ilara Vaskov",
            manual_origin=True,
            origin=Origin(kind=OriginKind.AUTHORED, authored_id="prefect_vaskov"),
        )
        derived = derive_origin(npc)
        assert derived.kind == OriginKind.AUTHORED
        assert derived.authored_id == "prefect_vaskov"


# ---------------------------------------------------------------------------
# Wiring — creation paths stamp typed provenance forward
# ---------------------------------------------------------------------------


class TestCreationPathsStampOrigin:
    def test_preload_authored_npcs_stamps_authored_with_authored_id(self) -> None:
        """§3a path 5: the session-start cast carries kind=AUTHORED and the
        ``AuthoredNpc.id`` — the id the dedup/purge/seating key needs (AC2).
        Today NOTHING marks an authored Npc (the survey's underivable case)."""
        from sidequest.game.origin import OriginKind
        from sidequest.game.world_materialization import preload_authored_npcs
        from sidequest.genre.models.authored_npc import AuthoredNpc

        snap = _snapshot()
        preload_authored_npcs(
            snap,
            [AuthoredNpc(id="prefect_vaskov", name="Prefect Ilara Vaskov")],
        )
        assert len(snap.npcs) == 1
        origin = snap.npcs[0].origin
        assert origin is not None, "authored preload left origin unstamped"
        assert origin.kind == OriginKind.AUTHORED
        assert origin.authored_id == "prefect_vaskov"

    def test_creature_patch_from_enemy_stamps_manual_pool(self) -> None:
        """§3a path 1 (encounter builder): the encountergen enemy row stamps
        MANUAL_POOL + creature_id."""
        from sidequest.game.origin import OriginKind
        from sidequest.server.dispatch.monster_manual_inject import (
            _creature_patch_from_enemy,
        )

        patch = _creature_patch_from_enemy(
            {"name": "Gnaw-Swarm", "creature_id": "gnaw_swarm", "hp": 6},
            tier=1,
            location=None,
        )
        assert patch is not None
        assert patch.origin is not None, "encounter patch left origin unstamped"
        assert patch.origin.kind == OriginKind.MANUAL_POOL
        assert patch.origin.creature_id == "gnaw_swarm"

    def test_creature_patch_from_bestiary_entry_stamps_room_bound(self) -> None:
        """§3a path 1 (room-binding builder, 107-2): byte-identical to an
        encounter patch in legacy fields — the stamp is the ONLY thing that
        distinguishes an authored room placement from pool filler."""
        from sidequest.game.origin import OriginKind
        from sidequest.server.dispatch.monster_manual_inject import (
            _creature_patch_from_bestiary_entry,
        )

        entry = SimpleNamespace(
            id="gnaw_swarm",
            name="Gnaw-Swarm",
            level=1,
            hp=6,
            abilities=[],
            description="A chittering carpet.",
            role="swarm",
        )
        patch = _creature_patch_from_bestiary_entry(entry, location=None)
        assert patch.origin is not None, "room-binding patch left origin unstamped"
        assert patch.origin.kind == OriginKind.ROOM_BOUND
        assert patch.origin.creature_id == "gnaw_swarm"

    def test_creature_patch_from_region_creature_stamps_region_population(self) -> None:
        """§3a path 7: the frozen procedural roster stamps REGION_POPULATION."""
        from sidequest.game.origin import OriginKind
        from sidequest.server.dispatch.monster_manual_inject import (
            _creature_patch_from_region_creature,
        )

        rc = SimpleNamespace(
            name="Pale Lurker",
            creature_type="pale_lurker",
            threat_level=2,
            hp=9,
            telegraph="Wet breathing.",
        )
        patch = _creature_patch_from_region_creature(rc, location=None, region="room_7")
        assert patch.origin is not None, "region-population patch left origin unstamped"
        assert patch.origin.kind == OriginKind.REGION_POPULATION
        assert patch.origin.creature_id == "pale_lurker"
