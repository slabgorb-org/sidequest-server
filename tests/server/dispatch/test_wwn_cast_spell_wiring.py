"""Wiring test: WWN cast_spell filter flows through the production
build_confrontation_payload + make_confrontation_frame_supplier path AND
the yield-projection path (handlers/yield_action.py call shape).

This is a fixture-driven behavior test, NOT a source-text grep — per
CLAUDE.md "No Source-Text Wiring Tests".  The test constructs a synthetic
WWN snapshot (Character with core.spellcasting), drives the real dispatch
function, and asserts behavior + OTEL span attributes.

The SpellcastingState derivation is centralized inside
``build_confrontation_payload`` (derived from
``core_resolver(recipient_actor_name).spellcasting`` when not explicitly
passed), so EVERY per-PC caller — panel projection AND the yield path —
gets WWN cast_spell gating without per-caller threading.

Acceptance:
- With casts_remaining > 0 and prepared non-empty → cast_spell in beats.
- With casts_remaining == 0 → cast_spell absent; span carries
  cast_spell_rejection_reason="no_slots".
- Centralized derivation: build_confrontation_payload with only
  core_resolver + recipient_actor_name (no explicit spellcasting) still
  applies the WWN gate.
- Yield path: resolve_recipient_pc + build_confrontation_payload (the
  exact yield_action.py:144-156 call shape) applies the WWN gate.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.wwn_magic import SpellcastingState
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import (
    BeatDef,
    BeatKind,
    ConfrontationDef,
    MetricDef,
)
from sidequest.server.dispatch.confrontation import (
    build_confrontation_payload,
    make_confrontation_frame_supplier,
    resolve_recipient_pc,
)
from sidequest.telemetry.spans.encounter import SPAN_CONFRONTATION_BEAT_FILTER

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """In-memory span exporter attached to the live TracerProvider."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _last_beat_filter_reason(exporter: InMemorySpanExporter) -> str | None:
    """Return cast_spell_rejection_reason from the most recent
    confrontation_beat_filter span, asserting at least one fired."""
    spans = [s for s in exporter.get_finished_spans() if s.name == SPAN_CONFRONTATION_BEAT_FILTER]
    assert spans, "confrontation_beat_filter_span must have been emitted"
    attrs = spans[-1].attributes
    assert attrs is not None
    value = attrs.get("cast_spell_rejection_reason")
    return value if value is None else str(value)


def _wwn_high_mage_class() -> ClassDef:
    return ClassDef(
        id="high_mage",
        display_name="High Mage",
        rpg_role="caster",
        jungian_default="sage",
        prime_requisite="INT",
        minimum_score=9,
        kit_table="high_mage_kit",
        flavor="Scholar of the arcane.",
        encounter_beat_choices=["cast_spell", "flee"],
    )


def _combat_cdef() -> ConfrontationDef:
    """Simple combat confrontation with cast_spell restricted to High Mage."""
    return ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="m", starting=0, threshold=7),
        opponent_metric=MetricDef(name="m", starting=0, threshold=7),
        beats=[
            BeatDef(id="strike", label="Strike", kind=BeatKind.strike, stat_check="STR"),
            BeatDef(
                id="cast_spell",
                label="Cast Spell",
                kind=BeatKind.strike,
                stat_check="INT",
                class_filter=["High Mage"],
            ),
        ],
    )


def _encounter_with_mage(mage_name: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="m", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="m", current=0, starting=0, threshold=7),
        actors=[
            EncounterActor(name=mage_name, role="combatant", side="player"),
            EncounterActor(name="Enemy", role="opponent", side="opponent"),
        ],
    )


def _mage_character(name: str, *, casts_remaining: int, prepared: list[str]) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="A mage.",
            personality="scholarly",
            hp=HpPool(current=6, max=6, base_max=6),
            spellcasting=SpellcastingState(
                prepared=prepared,
                casts_remaining=casts_remaining,
                casts_per_day=max(casts_remaining, 1),
                max_spell_level=1,
            ),
        ),
        backstory="A travelling scholar.",
        char_class="High Mage",
        race="Human",
    )


class _FakeGenrePack:
    """Minimal stub to satisfy resolve_recipient_pc's genre_pack.classes access
    and (Story 85-3) the portrait resolver's genre_pack.worlds access. `worlds`
    is empty → opponent portraits resolve to None, which is fine here: this
    suite asserts cast_spell filtering, not portraits."""

    def __init__(self, classes: list[ClassDef]) -> None:
        self.classes = classes
        self.worlds: dict[str, object] = {}


def _snapshot_with_mage(char: Character, player_id: str = "player_1") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="test_world",
    )
    snap.characters = [char]
    snap.player_seats = {player_id: char.core.name}
    return snap


# ---------------------------------------------------------------------------
# Part B — build_confrontation_payload direct wiring
# ---------------------------------------------------------------------------


def test_build_payload_wwn_cast_spell_selectable_with_spellcasting(
    otel_capture: InMemorySpanExporter,
) -> None:
    """build_confrontation_payload with spellcasting (casts>0, prepared
    non-empty) must include cast_spell in beats even when spell_slots=0.
    """
    mage = _wwn_high_mage_class()
    sc = SpellcastingState(
        prepared=["shatter"],
        casts_remaining=2,
        casts_per_day=2,
        max_spell_level=1,
    )
    payload = build_confrontation_payload(
        encounter=_encounter_with_mage("Lysa"),
        cdef=_combat_cdef(),
        genre_slug="elemental_harmony",
        recipient_pc=(mage, 0.0, None),  # B/X slots=0 — WWN arm must override
        recipient_actor_name="Lysa",
        spellcasting=sc,
    )
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" in beat_ids, (
        f"cast_spell must be in beats when spellcasting has casts; got {beat_ids}"
    )


def test_build_payload_wwn_cast_spell_filtered_no_casts_span_reason(
    otel_capture: InMemorySpanExporter,
) -> None:
    """build_confrontation_payload with casts_remaining==0 must filter
    cast_spell and emit span with cast_spell_rejection_reason='no_slots'.
    """
    mage = _wwn_high_mage_class()
    sc = SpellcastingState(
        prepared=["shatter"],
        casts_remaining=0,
        casts_per_day=2,
        max_spell_level=1,
    )
    payload = build_confrontation_payload(
        encounter=_encounter_with_mage("Lysa"),
        cdef=_combat_cdef(),
        genre_slug="elemental_harmony",
        recipient_pc=(mage, 99.0, None),  # B/X slots=99 — WWN arm must override
        recipient_actor_name="Lysa",
        spellcasting=sc,
    )
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" not in beat_ids, (
        f"cast_spell must be absent when casts_remaining==0; got {beat_ids}"
    )

    # Assert OTEL span carries the rejection reason
    reason = _last_beat_filter_reason(otel_capture)
    assert reason == "no_slots", (
        f"span must carry cast_spell_rejection_reason='no_slots'; got {reason!r}"
    )


# ---------------------------------------------------------------------------
# Part B — make_confrontation_frame_supplier wiring via snapshot
# ---------------------------------------------------------------------------


def test_frame_supplier_wwn_cast_spell_via_core_spellcasting(
    otel_capture: InMemorySpanExporter,
) -> None:
    """make_confrontation_frame_supplier must pick up spellcasting from
    core.spellcasting and include cast_spell when casts_remaining > 0.

    This exercises the full production path: resolve_recipient_pc →
    snapshot.find_creature_core → build_confrontation_payload → beat filter.
    """
    player_id = "player_1"
    char = _mage_character("Lysa", casts_remaining=2, prepared=["shatter"])
    snap = _snapshot_with_mage(char, player_id=player_id)
    genre_pack = _FakeGenrePack(classes=[_wwn_high_mage_class()])
    encounter = _encounter_with_mage("Lysa")
    cdef = _combat_cdef()

    supplier = make_confrontation_frame_supplier(
        snapshot=snap,
        genre_pack=genre_pack,
        encounter=encounter,
        cdef=cdef,
        genre_slug="elemental_harmony",
    )
    frame = supplier(player_id)
    assert frame is not None, "supplier must return a ConfrontationPayload for seated PC"
    beat_ids = [b["id"] for b in frame.beats]
    assert "cast_spell" in beat_ids, (
        f"cast_spell must be in frame beats when spellcasting has casts; got {beat_ids}"
    )


def test_frame_supplier_wwn_cast_spell_filtered_when_no_casts(
    otel_capture: InMemorySpanExporter,
) -> None:
    """make_confrontation_frame_supplier must filter cast_spell when
    core.spellcasting.casts_remaining == 0, and emit the rejection span.
    """
    player_id = "player_1"
    char = _mage_character("Lysa", casts_remaining=0, prepared=["shatter"])
    snap = _snapshot_with_mage(char, player_id=player_id)
    genre_pack = _FakeGenrePack(classes=[_wwn_high_mage_class()])
    encounter = _encounter_with_mage("Lysa")
    cdef = _combat_cdef()

    supplier = make_confrontation_frame_supplier(
        snapshot=snap,
        genre_pack=genre_pack,
        encounter=encounter,
        cdef=cdef,
        genre_slug="elemental_harmony",
    )
    frame = supplier(player_id)
    assert frame is not None
    beat_ids = [b["id"] for b in frame.beats]
    assert "cast_spell" not in beat_ids, (
        f"cast_spell must be absent when casts_remaining==0; got {beat_ids}"
    )

    # OTEL lie-detector: span must carry the rejection reason
    reason = _last_beat_filter_reason(otel_capture)
    assert reason == "no_slots", (
        f"span must carry cast_spell_rejection_reason='no_slots'; got {reason!r}"
    )


# ---------------------------------------------------------------------------
# Centralized derivation — build_confrontation_payload derives spellcasting
# from core_resolver + recipient_actor_name (no explicit spellcasting pass)
# ---------------------------------------------------------------------------


def test_build_payload_derives_spellcasting_from_core_resolver(
    otel_capture: InMemorySpanExporter,
) -> None:
    """No explicit ``spellcasting`` param: build_confrontation_payload must
    DERIVE it from ``core_resolver(recipient_actor_name).spellcasting`` and
    apply the WWN gate. This is the centralized derivation that auto-covers
    every per-PC caller.
    """
    mage = _wwn_high_mage_class()
    char = _mage_character("Lysa", casts_remaining=2, prepared=["shatter"])
    cores = {"Lysa": char.core}
    payload = build_confrontation_payload(
        encounter=_encounter_with_mage("Lysa"),
        cdef=_combat_cdef(),
        genre_slug="elemental_harmony",
        recipient_pc=(mage, 0.0, None),  # B/X slots=0; derived WWN arm must override
        recipient_actor_name="Lysa",
        core_resolver=lambda n: cores.get(n),
        # NOTE: no explicit spellcasting= — must be derived internally.
    )
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" in beat_ids, (
        f"cast_spell must be selectable via derived spellcasting; got {beat_ids}"
    )


def test_build_payload_derived_no_casts_filters_and_spans(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Derived spellcasting with casts_remaining==0 filters cast_spell and
    stamps the span reason — proving derivation feeds the rejection path."""
    mage = _wwn_high_mage_class()
    char = _mage_character("Lysa", casts_remaining=0, prepared=["shatter"])
    cores = {"Lysa": char.core}
    payload = build_confrontation_payload(
        encounter=_encounter_with_mage("Lysa"),
        cdef=_combat_cdef(),
        genre_slug="elemental_harmony",
        recipient_pc=(mage, 99.0, None),  # B/X slots=99; derived WWN arm must override
        recipient_actor_name="Lysa",
        core_resolver=lambda n: cores.get(n),
    )
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" not in beat_ids, (
        f"cast_spell must be filtered via derived spellcasting; got {beat_ids}"
    )
    assert _last_beat_filter_reason(otel_capture) == "no_slots"


def test_explicit_spellcasting_overrides_derivation() -> None:
    """Explicit ``spellcasting`` wins over the core-derived value — the
    override contract. A core with casts=0 but an explicit casts>0 state
    selects cast_spell.
    """
    mage = _wwn_high_mage_class()
    depleted_core = _mage_character("Lysa", casts_remaining=0, prepared=["shatter"]).core
    cores = {"Lysa": depleted_core}
    explicit = SpellcastingState(
        prepared=["shatter"], casts_remaining=3, casts_per_day=3, max_spell_level=1
    )
    payload = build_confrontation_payload(
        encounter=_encounter_with_mage("Lysa"),
        cdef=_combat_cdef(),
        genre_slug="elemental_harmony",
        recipient_pc=(mage, 0.0, None),
        recipient_actor_name="Lysa",
        core_resolver=lambda n: cores.get(n),
        spellcasting=explicit,  # explicit wins over depleted core
    )
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" in beat_ids, (
        f"explicit spellcasting (casts>0) must override depleted core; got {beat_ids}"
    )


def test_bx_core_with_none_spellcasting_unchanged() -> None:
    """A B/X recipient core has ``spellcasting is None`` → derived value is
    None → the B/X slot arm governs unchanged (byte-for-byte). With slots=0
    and no spellcasting, cast_spell is filtered by the B/X gate.
    """
    mage = _wwn_high_mage_class()  # class allows cast_spell
    bx_core = CreatureCore(
        name="Lysa",
        description="A mage.",
        personality="scholarly",
        hp=HpPool(current=6, max=6, base_max=6),
        # spellcasting defaults to None → B/X arm
    )
    assert bx_core.spellcasting is None
    cores = {"Lysa": bx_core}
    payload = build_confrontation_payload(
        encounter=_encounter_with_mage("Lysa"),
        cdef=_combat_cdef(),
        genre_slug="caverns_and_claudes",
        recipient_pc=(mage, 0.0, None),  # B/X slots=0 → B/X gate filters cast_spell
        recipient_actor_name="Lysa",
        core_resolver=lambda n: cores.get(n),
    )
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" not in beat_ids, (
        f"B/X core (spellcasting=None, slots=0) must filter cast_spell; got {beat_ids}"
    )


# ---------------------------------------------------------------------------
# Yield-projection path — the exact handlers/yield_action.py:144-156 shape
# ---------------------------------------------------------------------------


def test_yield_path_wwn_cast_spell_offered_when_casts_available(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Yield-projection path: resolve_recipient_pc + build_confrontation_payload
    (the yield_action.py call shape) must offer cast_spell to a WWN caster
    with casts_remaining>0 / prepared non-empty.

    A WWN caster has NO MagicState, so resolve_recipient_pc returns
    spell_slots=0.0 / prepared=None — exactly the B/X shape that would
    WRONGLY filter cast_spell pre-fix. The centralized derivation rescues it.
    """
    player_id = "player_1"
    char = _mage_character("Lysa", casts_remaining=2, prepared=["shatter"])
    snap = _snapshot_with_mage(char, player_id=player_id)
    genre_pack = _FakeGenrePack(classes=[_wwn_high_mage_class()])
    encounter = _encounter_with_mage("Lysa")
    cdef = _combat_cdef()

    # Exact production call shape from handlers/yield_action.py:144-156.
    recipient_pc, recipient_actor = resolve_recipient_pc(
        snapshot=snap,
        genre_pack=genre_pack,
        player_id=player_id,
    )
    assert recipient_pc is not None, "WWN mage must resolve as a recipient PC"
    # Confirm the B/X shape that would have mis-filtered pre-fix.
    _cls, slots, prepared = recipient_pc
    assert slots == 0.0 and prepared is None, (
        "WWN caster has no MagicState → B/X slots=0/prepared=None (the trap)"
    )

    payload_dict = build_confrontation_payload(
        encounter=encounter,
        cdef=cdef,
        genre_slug="elemental_harmony",
        recipient_pc=recipient_pc,
        recipient_actor_name=recipient_actor,
        core_resolver=snap.find_creature_core,
    )
    beat_ids = [b["id"] for b in payload_dict["beats"]]
    assert "cast_spell" in beat_ids, (
        f"yield-path WWN caster with casts must see cast_spell; got {beat_ids}"
    )


def test_yield_path_wwn_cast_spell_filtered_no_casts_span_reason(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Yield-projection path: WWN caster with casts_remaining==0 must have
    cast_spell filtered and the span carry reason 'no_slots'.
    """
    player_id = "player_1"
    char = _mage_character("Lysa", casts_remaining=0, prepared=["shatter"])
    snap = _snapshot_with_mage(char, player_id=player_id)
    genre_pack = _FakeGenrePack(classes=[_wwn_high_mage_class()])
    encounter = _encounter_with_mage("Lysa")
    cdef = _combat_cdef()

    recipient_pc, recipient_actor = resolve_recipient_pc(
        snapshot=snap,
        genre_pack=genre_pack,
        player_id=player_id,
    )
    assert recipient_pc is not None
    payload_dict = build_confrontation_payload(
        encounter=encounter,
        cdef=cdef,
        genre_slug="elemental_harmony",
        recipient_pc=recipient_pc,
        recipient_actor_name=recipient_actor,
        core_resolver=snap.find_creature_core,
    )
    beat_ids = [b["id"] for b in payload_dict["beats"]]
    assert "cast_spell" not in beat_ids, (
        f"yield-path WWN caster with no casts must NOT see cast_spell; got {beat_ids}"
    )
    reason = _last_beat_filter_reason(otel_capture)
    assert reason == "no_slots", (
        f"yield-path span must carry cast_spell_rejection_reason='no_slots'; got {reason!r}"
    )
