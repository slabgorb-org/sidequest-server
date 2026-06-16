"""RED (story 114-10): chargen gear compile + the refresh invariant.

The design's chargen compile step (``2026-06-15-fate-gear-model-design.md`` §"Chargen
compile + the refresh invariant"): for the chosen archetype, resolve its ``gear:
[ids]`` against the genre-tier ``GearDef`` set and MATERIALIZE each grant onto the
character's ``FateSheet`` — each ``grants_aspects`` entry becomes an ``Aspect``
(carrying its ``kind`` + ``source_gear``), each ``grants_stunts`` entry a ``Stunt``
(carrying ``source_gear``). Then compute the sheet's ``refresh`` from the invariant:

    refresh == base_refresh − max(0, total_stunts − free_stunts)

where ``total_stunts`` = authored stunts already on the sheet + gear-granted stunts.

The whole balance story (SOUL → Bind the Ruleset, Don't Balance It):
  - aspect-gear and permission-gear are FREE — they never debit refresh.
  - stunt-gear MUST debit refresh once the free-stunt allotment is exhausted.

Unknown gear id fails loud (No Silent Fallbacks). The compile emits
``fate.gear_compiled`` (asserted structurally here; the span's own contract is in
``tests/telemetry/test_fate_gear_compiled_span.py``).

Contract under test — a compile entry point Dev implements (the import fails in RED):

    from sidequest.game.ruleset.fate_gear import compile_gear_onto_sheet

    result = compile_gear_onto_sheet(
        sheet,                       # FateSheet to mutate
        archetype="The Gumshoe",
        gear_ids=[...],              # archetype.gear
        gear_defs=[...],             # genre-tier GearDef set
        base_refresh=3,
        free_stunts=3,
        actor="Sam",
        _tracer=None,
    )                                # -> GearCompileResult(refresh_before/after/debited, ...)
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.fate_sheet import FateSheet, Stunt

# NEW in 114-10 — fails in RED until Dev adds the compile module.
from sidequest.game.ruleset.fate_gear import compile_gear_onto_sheet
from sidequest.genre.models.inventory import GearDef, GearGrantAspect, GearGrantStunt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TRENCHCOAT = GearDef(
    id="noir_trenchcoat",
    name="Trench Coat",
    grants_aspects=[GearGrantAspect(text="Collar Always Up", kind="character")],
)
LICENSE = GearDef(
    id="noir_license",
    name="PI License",
    grants_aspects=[GearGrantAspect(text="Licensed Investigator", kind="permission")],
)
LIGHTER_STUNT = GearDef(
    id="noir_lighter",
    name="Trusty Lighter",
    grants_stunts=[GearGrantStunt(name="Always Has a Light")],
)
GADGET_BELT = GearDef(
    id="gadget_belt",
    name="Gadget Belt",
    grants_stunts=[
        GearGrantStunt(name="Smoke Bomb"),
        GearGrantStunt(name="Grappling Hook"),
        GearGrantStunt(name="Flash Powder"),
        GearGrantStunt(name="Lockpicks"),
    ],
)

ALL_GEAR = [TRENCHCOAT, LICENSE, LIGHTER_STUNT, GADGET_BELT]


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _compile(sheet, gear_ids, *, base_refresh=3, free_stunts=3, gear_defs=ALL_GEAR, tracer=None):
    return compile_gear_onto_sheet(
        sheet,
        archetype="Test",
        gear_ids=list(gear_ids),
        gear_defs=list(gear_defs),
        base_refresh=base_refresh,
        free_stunts=free_stunts,
        actor="Tester",
        _tracer=tracer,
    )


# ---------------------------------------------------------------------------
# Materialization — aspects + stunts land on the sheet with source_gear
# ---------------------------------------------------------------------------


class TestMaterialization:
    def test_aspect_gear_appends_aspect_with_kind_and_source_gear(self) -> None:
        sheet = FateSheet()
        _compile(sheet, ["noir_trenchcoat"])
        match = [a for a in sheet.aspects if a.text == "Collar Always Up"]
        assert len(match) == 1, "the gear's aspect must be appended to the sheet"
        assert match[0].kind == "character"
        assert match[0].source_gear == "noir_trenchcoat", (
            "every materialized aspect must be stamped with its source gear id (A2-i)"
        )

    def test_permission_gear_preserves_permission_kind(self) -> None:
        sheet = FateSheet()
        _compile(sheet, ["noir_license"])
        lic = next(a for a in sheet.aspects if a.text == "Licensed Investigator")
        assert lic.kind == "permission", "a permission grant must land as a permission aspect (P-i)"
        assert lic.source_gear == "noir_license"

    def test_stunt_gear_appends_stunt_with_source_gear(self) -> None:
        sheet = FateSheet()
        _compile(sheet, ["noir_lighter"])
        stunt = next(s for s in sheet.stunts if s.name == "Always Has a Light")
        assert stunt.source_gear == "noir_lighter"

    def test_multiple_gear_ids_all_materialize(self) -> None:
        sheet = FateSheet()
        _compile(sheet, ["noir_trenchcoat", "noir_license", "noir_lighter"])
        assert {a.source_gear for a in sheet.aspects if a.source_gear} == {
            "noir_trenchcoat",
            "noir_license",
        }
        assert any(s.source_gear == "noir_lighter" for s in sheet.stunts)

    def test_result_reports_materialization_counters(self) -> None:
        # The returned GearCompileResult counters are the span's source of truth
        # (the GM-panel lie detector reads them). Assert them directly so a
        # 0-count / mis-count regression in the compile loop can't slip through:
        # trenchcoat(1 character aspect) + license(1 permission aspect) +
        # lighter(1 stunt) → 2 aspects placed, 1 of them permission, 1 stunt added.
        sheet = FateSheet()
        result = _compile(sheet, ["noir_trenchcoat", "noir_license", "noir_lighter"])
        assert result.gear_ids == ("noir_trenchcoat", "noir_license", "noir_lighter")
        assert result.aspects_placed == 2
        assert result.permission_aspects == 1
        assert result.stunts_added == 1


# ---------------------------------------------------------------------------
# The refresh invariant — aspect-gear free, stunt-gear debits
# ---------------------------------------------------------------------------


class TestRefreshInvariant:
    def test_aspect_only_gear_does_not_debit_refresh(self) -> None:
        # The coat + the badge are free; refresh stays at base_refresh.
        sheet = FateSheet()
        result = _compile(sheet, ["noir_trenchcoat", "noir_license"], base_refresh=3, free_stunts=3)
        assert sheet.refresh == 3
        assert result.refresh_debited == 0

    def test_stunt_gear_within_free_allotment_does_not_debit(self) -> None:
        # One stunt, three free → no debit.
        sheet = FateSheet()
        result = _compile(sheet, ["noir_lighter"], base_refresh=3, free_stunts=3)
        assert sheet.refresh == 3
        assert result.refresh_debited == 0

    def test_stunt_gear_exceeding_free_allotment_debits_refresh(self) -> None:
        # gadget_belt grants 4 stunts; free_stunts=3 → 1 over → refresh 3 - 1 = 2.
        sheet = FateSheet()
        result = _compile(sheet, ["gadget_belt"], base_refresh=3, free_stunts=3)
        assert sheet.refresh == 2, "total_stunts(4) − free_stunts(3) = 1 refresh debited"
        assert result.refresh_before == 3
        assert result.refresh_after == 2
        assert result.refresh_debited == 1

    def test_authored_stunts_count_toward_total(self) -> None:
        # A sheet that already carries 2 authored stunts + 2 gear stunts = 4 total;
        # with free_stunts=3 that is 1 over → refresh debited by 1.
        sheet = FateSheet(stunts=[Stunt(name="Tough"), Stunt(name="Quick")])
        result = _compile(
            sheet,
            ["noir_lighter"],  # +1 gear stunt → 3 total; still within free=3? see below
            base_refresh=3,
            free_stunts=2,
        )
        # authored 2 + gear 1 = 3 total; free_stunts=2 → 1 over → debit 1.
        assert result.refresh_debited == 1
        assert sheet.refresh == 2

    def test_refresh_floors_per_srd(self) -> None:
        # Even a pathological pile of stunt-gear cannot drive refresh below 1
        # (SRD floor) — the binding never produces an illegal sheet. base=2,
        # free=0, 4 gear-stunts → 2 − 4 = −2, clamped to exactly 1. Assert the
        # exact floor value: a bug that skipped the clamp (returning base_refresh=2,
        # or a raw negative) would slip past a `>= 1` assertion.
        sheet = FateSheet()
        result = _compile(sheet, ["gadget_belt"], base_refresh=2, free_stunts=0)
        assert sheet.refresh == 1
        assert result.refresh_after == 1
        # refresh_debited is the ACTUAL (post-floor) reduction: 2 − 1 = 1.
        assert result.refresh_debited == 1


# ---------------------------------------------------------------------------
# Fail loud — unknown gear id (No Silent Fallbacks)
# ---------------------------------------------------------------------------


class TestUnknownGearFailsLoud:
    def test_unknown_gear_id_raises(self) -> None:
        sheet = FateSheet()
        with pytest.raises(ValueError, match="ghost_gun"):
            _compile(sheet, ["ghost_gun"])

    def test_unknown_id_does_not_partially_mutate(self) -> None:
        # Fail BEFORE materializing ANY valid sibling — no half-applied sheet,
        # neither aspects NOR stunts. noir_lighter is a stunt-gear sibling that
        # precedes the bad id, so the stunt path is visible: an incremental
        # (non-resolve-first) compile would have appended its Stunt before
        # ghost_gun raised. The sheet started empty, so the strongest proof is
        # that nothing at all was appended.
        sheet = FateSheet()
        with pytest.raises(ValueError, match="ghost_gun"):
            _compile(sheet, ["noir_trenchcoat", "noir_lighter", "ghost_gun"])
        # The sheet started empty, so exact emptiness is the strongest proof of
        # no partial mutation — it subsumes any per-entry source_gear check (a
        # universal-quantifier over an empty list is vacuously true).
        assert sheet.aspects == [], "no aspect may be appended by a failed compile"
        assert sheet.stunts == [], "no stunt may be appended by a failed compile"


# ---------------------------------------------------------------------------
# Wiring — the compile emits the lie-detector span
# ---------------------------------------------------------------------------


class TestEmitsSpan:
    def test_compile_emits_fate_gear_compiled_span(self) -> None:
        exporter, tracer = _otel()
        sheet = FateSheet()
        _compile(sheet, ["gadget_belt"], base_refresh=3, free_stunts=3, tracer=tracer)
        names = {s.name for s in exporter.get_finished_spans()}
        assert "fate.gear_compiled" in names, (
            "compile_gear_onto_sheet must emit fate.gear_compiled (GM-panel lie detector)"
        )
