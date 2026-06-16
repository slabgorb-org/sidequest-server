"""RED (story 114-10 / ADR-144 §D5-seam, ADR-145 §D5): Fate gear MODEL deltas.

The 114-9 design (``docs/superpowers/specs/2026-06-15-fate-gear-model-design.md``)
adds a thin authoring shim that compiles starting gear into the existing
``FateSheet`` at chargen. This file pins the *data model* half of that contract —
the two deliberate engine deltas plus the three new content models — independent
of the compile step (``test_fate_gear_compile.py``) and the OTEL span
(``test_fate_gear_compiled_span.py``).

Two engine deltas (``fate_sheet.py``):
  1. ``AspectKind`` gains ``"permission"`` (a narrator-read capability kind, never
     an engine gate — P-i / The Zork Problem).
  2. ``Aspect`` and ``Stunt`` gain ``source_gear: str | None`` (traceability for the
     GM-panel lie-detector and "you lost the coat" beats — A2-i).

Three content models (``genre/models/inventory.py``, ALONGSIDE the WN-shaped
``CatalogItem`` which stays untouched): ``GearGrantAspect``, ``GearGrantStunt``,
``GearDef``. Fate has no equipment economy, so ``GearDef`` carries NO value /
weight / damage / provenance fields.

One rules delta (``genre/models/rules.py``): ``FateConfig`` gains ``base_refresh``
and ``free_stunts`` — the two numbers the refresh invariant reads.

The module-level imports of the NEW symbols fail collection in RED; that import
failure is the first signal Dev must satisfy (the 121-1 precedent).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.fate_sheet import Aspect, Stunt

# NEW in 114-10 — these imports fail in RED until Dev adds the models.
from sidequest.genre.models.inventory import (
    GearDef,
    GearGrantAspect,
    GearGrantStunt,
)
from sidequest.genre.models.rules import FateConfig


# ---------------------------------------------------------------------------
# Engine delta 1 — AspectKind gains "permission"
# ---------------------------------------------------------------------------


class TestAspectPermissionKind:
    def test_permission_is_a_valid_aspect_kind(self) -> None:
        # P-i: a permission compiles to an aspect of a new "permission" kind.
        aspect = Aspect(text="Private Investigator's License", kind="permission")
        assert aspect.kind == "permission"

    def test_existing_aspect_kinds_still_valid(self) -> None:
        # The delta is ADDITIVE — it must not drop any SRD aspect kind.
        for kind in ("high_concept", "trouble", "character", "situation", "consequence", "boost"):
            assert Aspect(text="x", kind=kind).kind == kind

    def test_unknown_aspect_kind_still_rejected(self) -> None:
        # Adding "permission" must not loosen the Literal into a free string.
        with pytest.raises(ValidationError):
            Aspect(text="x", kind="teleport")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Engine delta 2 — Aspect/Stunt gain source_gear
# ---------------------------------------------------------------------------


class TestSourceGearBackref:
    def test_aspect_source_gear_defaults_none(self) -> None:
        # Hand-authored (non-gear) aspects carry source_gear == None.
        assert Aspect(text="Hard-Boiled", kind="high_concept").source_gear is None

    def test_aspect_source_gear_accepts_gear_id(self) -> None:
        a = Aspect(text="Collar Always Up", kind="character", source_gear="noir_trenchcoat")
        assert a.source_gear == "noir_trenchcoat"

    def test_stunt_source_gear_defaults_none(self) -> None:
        assert Stunt(name="Fast Draw").source_gear is None

    def test_stunt_source_gear_accepts_gear_id(self) -> None:
        s = Stunt(name="Always Has a Lighter", source_gear="noir_lighter")
        assert s.source_gear == "noir_lighter"

    def test_source_gear_is_first_class_not_loose_metadata(self) -> None:
        # extra="forbid" must still hold — source_gear is a typed field, and
        # an unknown field is still rejected (it didn't become extra="allow").
        with pytest.raises(ValidationError):
            Aspect(text="x", kind="character", bogus_field="y")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Content model — GearGrantAspect
# ---------------------------------------------------------------------------


class TestGearGrantAspect:
    def test_defaults_to_character_kind(self) -> None:
        g = GearGrantAspect(text="Collar Always Up")
        assert g.kind == "character"

    def test_accepts_permission_kind(self) -> None:
        g = GearGrantAspect(text="Badge Lets You Order Constables", kind="permission")
        assert g.kind == "permission"

    def test_rejects_non_character_non_permission_kind(self) -> None:
        # The grant kind Literal is narrower than AspectKind: only character |
        # permission may be AUTHORED on gear (a gear item can't grant a
        # high_concept / consequence / boost).
        with pytest.raises(ValidationError):
            GearGrantAspect(text="x", kind="high_concept")  # type: ignore[arg-type]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GearGrantAspect(text="x", free_invokes=2)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Content model — GearGrantStunt
# ---------------------------------------------------------------------------


class TestGearGrantStunt:
    def test_description_defaults_empty(self) -> None:
        s = GearGrantStunt(name="Fast Draw")
        assert s.description == ""

    def test_has_no_cost_field(self) -> None:
        # Design A2-i: there is NO per-stunt cost field — a Fate stunt costs
        # exactly one refresh, accounted by the stunt COUNT against free_stunts.
        # A redundant cost field is rejected by extra="forbid".
        with pytest.raises(ValidationError):
            GearGrantStunt(name="Fast Draw", cost=1)  # type: ignore[call-arg]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GearGrantStunt(name="x", refresh=2)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Content model — GearDef
# ---------------------------------------------------------------------------


class TestGearDef:
    def test_minimal_gear_is_pure_flavor(self) -> None:
        # A gear item with neither grant is legal (a hat is a hat).
        g = GearDef(id="noir_fedora", name="Battered Fedora")
        assert g.id == "noir_fedora"
        assert g.grants_aspects == []
        assert g.grants_stunts == []

    def test_grants_aspects_and_stunts(self) -> None:
        g = GearDef(
            id="noir_trenchcoat",
            name="Trench Coat",
            description="Collar always up.",
            grants_aspects=[GearGrantAspect(text="Collar Always Up")],
            grants_stunts=[GearGrantStunt(name="Always Has a Lighter")],
        )
        assert g.grants_aspects[0].text == "Collar Always Up"
        assert g.grants_stunts[0].name == "Always Has a Lighter"

    @pytest.mark.parametrize("economy_field", ["value", "weight", "damage", "provenance", "rarity"])
    def test_no_equipment_economy_fields(self, economy_field: str) -> None:
        # Fate has no economy: GearDef must NOT accept any CatalogItem-style
        # priced/weighted/damage/provenance field (no union rot — A2-i).
        with pytest.raises(ValidationError):
            GearDef(id="x", name="X", **{economy_field: 1})  # type: ignore[arg-type]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GearDef(id="x", name="X", findable=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Rules delta — FateConfig gains base_refresh + free_stunts
# ---------------------------------------------------------------------------


class TestFateConfigRefreshKnobs:
    def test_accepts_base_refresh_and_free_stunts(self) -> None:
        # The fate: block in rules.yaml carries these per pack (genre tone);
        # the refresh invariant reads them. extra="forbid" rejects them in RED.
        cfg = FateConfig(skills={"Shoot": 3}, base_refresh=3, free_stunts=3)
        assert cfg.base_refresh == 3
        assert cfg.free_stunts == 3

    def test_base_refresh_defaults_to_srd_three(self) -> None:
        # Design resolved-decision 3: defaulting to the SRD 3/3 when omitted,
        # NOT hardcoded globally — the field exists with a 3 default.
        cfg = FateConfig(skills={"Shoot": 3})
        assert cfg.base_refresh == 3
        assert cfg.free_stunts == 3
