"""Story 162-3 — bestiary ``generics:`` schema + the GENERIC origin kind.

The survey (docs/superpowers/specs/2026-07-05-npc-generation-inventory.md §4
conflict #2 / §8 D6) ends the six-strategy seating stack's last resort — the
ephemeral stub mint — and replaces it with an AUTHORED source: a ``generics:``
section in bestiary.yaml. Generic rows are full ``BestiaryEntry`` stat blocks
("goblin", "street tough", ...) authored per world (or genre tier), and they
become the sanctioned last-resort Other for the origin-precedence path:

    authored > room-bound > region-population > MM pool > GENERICS > error

This file pins the CONTENT-FACING contract:

  1. ``Bestiary`` accepts an optional ``generics:`` section (extra="forbid"
     today → clean ValidationError RED).
  2. Generic rows are full ``BestiaryEntry`` stat blocks — same required
     combat fields, same fail-loud row validation (empty id, missing hp).
  3. ``generics`` is optional and defaults to ``[]`` — every shipped
     bestiary.yaml keeps loading unchanged.
  4. ids stay unique ACROSS entries + generics — identity is id-keyed
     (162-2 ``identity_key``); a cross-section id collision forks identity.
  5. ``OriginKind`` gains ``GENERIC`` and it round-trips on ``Npc.origin``.
  6. Legacy derivation is untouched: an old-save ephemeral stub still derives
     ``EPHEMERAL_STUB`` (green guard — must keep holding through GREEN).

Seating behavior lives in tests/server/test_162_3_generics_last_resort_seating.py;
shipped-content population lives in tests/genre/test_162_3_generics_content.py.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry


def _row(id_: str = "wight", name: str = "Wight of the Dead Watch", **over) -> dict:
    base = {
        "id": id_,
        "name": name,
        "level": 3,
        "hp": 14,
        "armor_class": 15,
        "attack_bonus": 3,
        "damage": "1d8",
    }
    base.update(over)
    return base


def _generic_row(**over) -> dict:
    base = {
        "id": "hold_dead",
        "name": "Hold-Dead",
        "level": 1,
        "hp": 6,
        "armor_class": 11,
        "attack_bonus": 1,
        "damage": "1d6",
        "role": "risen dwarfhold laborer, generic Other",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. The generics section is accepted and typed
# ---------------------------------------------------------------------------


class TestGenericsSectionAccepted:
    def test_bestiary_accepts_generics_section(self) -> None:
        """AC1: ``generics:`` validates alongside ``entries:`` and the rows come
        back as typed ``BestiaryEntry`` objects. RED today: ``Bestiary`` is
        extra="forbid" with only ``entries`` — extra_forbidden."""
        b = Bestiary.model_validate({"entries": [_row()], "generics": [_generic_row()]})
        generics = b.generics
        assert len(generics) == 1
        g = generics[0]
        assert isinstance(g, BestiaryEntry)
        assert g.id == "hold_dead"
        assert g.hp == 6
        assert g.armor_class == 11

    def test_world_bestiary_yaml_shape_with_generics_parses(self) -> None:
        """The authoring surface: a world bestiary.yaml mirroring the shipped
        beneath_sunden shape (extra SRD color keys on rows) plus a ``generics:``
        section parses through the same model the loader calls."""
        doc = yaml.safe_load(
            """
            entries:
              - id: wight
                name: Wight of the Dead Watch
                level: 3
                hp: 14
                armor_class: 15
                attack_bonus: 3
                damage: "1d8"
                move: "30 ft."
                morale: 12
                tags: [undead, mid]
            generics:
              - id: hold_dead
                name: Hold-Dead
                level: 1
                hp: 6
                armor_class: 11
                attack_bonus: 1
                damage: "1d6"
                morale: 7
                role: risen dwarfhold laborer
            """
        )
        b = Bestiary.model_validate(doc)
        assert [g.id for g in b.generics] == ["hold_dead"]

    def test_generics_omitted_defaults_to_empty_list(self) -> None:
        """AC1 backward-compat: every shipped bestiary.yaml (no ``generics:``)
        keeps loading, and the section reads as an EMPTY list — never None,
        never an AttributeError. RED today: the field does not exist."""
        b = Bestiary.model_validate({"entries": [_row()]})
        assert b.generics == []


# ---------------------------------------------------------------------------
# 2. Generic rows are full, fail-loud BestiaryEntry stat blocks
# ---------------------------------------------------------------------------


class TestGenericRowValidation:
    def test_generic_row_missing_hp_rejected(self) -> None:
        """A generic row is a REAL stat block, not a name list — the required
        combat fields (hp et al.) are enforced per row. Asserted on the error
        ``loc`` so this stays RED today (today the failure is extra_forbidden
        on ``generics`` itself, which never mentions ``hp``)."""
        row = _generic_row()
        del row["hp"]
        with pytest.raises(ValidationError) as ei:
            Bestiary.model_validate({"entries": [_row()], "generics": [row]})
        assert any("hp" in e["loc"] for e in ei.value.errors()), (
            f"expected a per-row missing-hp error under generics; got {ei.value.errors()!r}"
        )

    def test_generic_row_empty_id_rejected(self) -> None:
        """Row-level fail-loud carries over: an empty id in generics raises the
        same loud message the entries rows raise (space-containing phrase —
        cannot be satisfied by a path fragment)."""
        with pytest.raises(ValidationError, match="id must not be empty"):
            Bestiary.model_validate({"entries": [_row()], "generics": [_generic_row(id="")]})

    def test_duplicate_id_across_entries_and_generics_rejected(self) -> None:
        """Identity is id-keyed (162-2 ``identity_key``: ``creature:<id>``). One
        id living in BOTH sections would make two divergent stat blocks the
        same identity — the load must refuse, loudly, naming the id."""
        with pytest.raises(ValidationError, match="duplicate"):
            Bestiary.model_validate(
                {
                    "entries": [_row(id_="hold_dead", name="Hold-Dead Elite")],
                    "generics": [_generic_row(id="hold_dead")],
                }
            )

    def test_duplicate_id_within_generics_rejected(self) -> None:
        """Same uniqueness rule the entries section already enforces."""
        with pytest.raises(ValidationError, match="duplicate"):
            Bestiary.model_validate(
                {
                    "entries": [_row()],
                    "generics": [_generic_row(), _generic_row(name="Hold-Dead Twin")],
                }
            )

    def test_unknown_top_level_key_still_rejected(self) -> None:
        """Green-on-arrival guard (schema check #8): adding ``generics`` must
        not loosen the top-level extra="forbid" — a typo'd ``generic:`` key
        (singular) keeps failing loud instead of silently vanishing."""
        with pytest.raises(ValidationError):
            Bestiary.model_validate({"entries": [_row()], "generic": [_generic_row()]})


# ---------------------------------------------------------------------------
# 3. OriginKind.GENERIC — the sanctioned last-resort provenance stamp
# ---------------------------------------------------------------------------


class TestGenericOriginKind:
    def test_originkind_has_generic_variant(self) -> None:
        """162-3 adds the seventh creation family: an Other seated from the
        bestiary generics section. RED today: no such variant."""
        from sidequest.game.origin import OriginKind

        assert OriginKind.GENERIC.value == "generic"

    def test_origin_generic_roundtrips_on_npc(self) -> None:
        """A GENERIC stamp (kind + the generic row's creature_id) survives the
        Npc JSON round-trip — provenance is durable save state, not a runtime
        guess (162-1 derive-don't-cache doctrine extended by 162-2)."""
        from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
        from sidequest.game.origin import Origin, OriginKind
        from sidequest.game.session import Npc

        npc = Npc(
            core=CreatureCore(
                name="Hold-Dead",
                description="A risen laborer.",
                personality="Relentless.",
                inventory=Inventory(),
                hp=HpPool(current=6, max=6, base_max=6),
                armor_class=11,
            ),
            creature_id="hold_dead",
            origin=Origin(kind=OriginKind.GENERIC, creature_id="hold_dead"),
        )
        revived = Npc.model_validate(npc.model_dump(mode="json"))
        assert revived.origin is not None
        assert revived.origin.kind == OriginKind.GENERIC
        assert revived.origin.creature_id == "hold_dead"

    def test_identity_key_for_generic_origin_is_name_keyed(self) -> None:
        """SUPERSEDED by ADR-156 Amendment B (Green Room, accepted 2026-07-11):
        this test originally pinned the generic row id as the identity key —
        "two prose names over the same generic row are ONE identity". That
        was wrong: a ``generics:`` bestiary row is a stat DONOR shared by many
        seatings, not an identity. "Gruk the Smasher" and "The Pale Digger"
        drawn from the same ``hold_dead`` stat block are two DIFFERENT people
        who happen to share a chassis — collapsing them into one identity was
        the two-names-one-enemy fork in reverse (many people, one phantom).
        GENERIC now keys on the normalized display name, like an id-less
        origin (see sidequest.game.origin.identity_key and
        test_162_2_origin_model.py::test_identity_key_generic_kind_keys_by_name)."""
        from sidequest.game.origin import Origin, OriginKind, identity_key

        origin = Origin(kind=OriginKind.GENERIC, creature_id="hold_dead")
        assert identity_key(origin, "Gruk the Smasher") == "name:gruk the smasher"
        assert identity_key(origin, "The Pale Digger") == "name:the pale digger"
        assert identity_key(origin, "Gruk the Smasher") != identity_key(
            origin, "The Pale Digger"
        )

    def test_legacy_ephemeral_stub_still_derives_ephemeral_stub(self) -> None:
        """Green guard (must hold through GREEN): 162-3 removes stub MINTING on
        the seater path, not the legacy derivation — a pre-162-3 save carrying
        an unstamped ephemeral stub still derives EPHEMERAL_STUB so forensics
        and the reap keep seeing the truth."""
        from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
        from sidequest.game.origin import OriginKind, derive_origin
        from sidequest.game.session import Npc

        legacy = Npc(
            core=CreatureCore(
                name="Arena Opponent",
                description="Combat opponent",
                personality="Adversary",
                inventory=Inventory(),
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            ephemeral=True,
        )
        assert derive_origin(legacy).kind == OriginKind.EPHEMERAL_STUB
