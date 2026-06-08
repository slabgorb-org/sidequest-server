"""barsoom chargen wiring — TDD RED for story 89-5.

Wiring proof (CLAUDE.md: "Verify Wiring, Not Just Existence") that the
Barsoom chargen surface reaches the REAL ``CharacterBuilder.build()``:

  * an Earthman build carries the gravity boon end-to-end — the STR edge
    lands in the production accumulator (``stat_bonuses``, consumed
    additively by ``generate_stats`` under every strategy) AND the leap
    ability lands on ``Character.abilities`` with ``source == Race`` and a
    non-empty engine-facing ``mechanical_effect`` (Keith's call: "STR edge
    + leap ability");
  * applying the boon emits the ``chargen.origin_trait.applied`` OTEL
    event (the GM-panel lie-detector contract — no silent crunch);
  * a native-origin build gets NONE of that (four arms fiction-only);
  * the boon is WORLD-TIER: a genre-tier (non-barsoom) build with
    ``race_hint == "Earthman"`` injected does NOT receive the boon — the
    trait definition must live in barsoom world content, not as an
    engine/genre hardcode keyed off the race string;
  * Mentalist + Super-scientist builds through the BARSOOM world scenes
    seed populated WWN SpellcastingState (the 89-4 starting_prepared
    seeds become real prepared spells at the table).

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The OTEL event contract for the origin-trait application (mirrors
# chargen.class_abilities.seeded — see telemetry/spans/chargen.py).
ORIGIN_TRAIT_EVENT = "chargen.origin_trait.applied"

# Keith's 89-5 calls.
_EARTHMAN_STR_BONUS = 2
_LEAP_NAME_MARKERS = ("leap", "gravity")


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _fresh_otel() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _events_by_name(exporter: InMemorySpanExporter) -> dict[str, list]:
    result: dict[str, list] = {}
    for span in exporter.get_finished_spans():
        for event in span.events:
            result.setdefault(event.name, []).append(event)
    return result


def _walk_and_build(
    pack,
    name: str,
    *,
    world_slug: str | None = "barsoom",
    race_display: str | None = None,
    class_display: str | None = None,
    inject_race_if_unoffered: bool = False,
):
    """Walk the resolved chargen scenes via the REAL builder.

    At each choice-bearing scene, prefer (in order) an unmatched race target,
    then an unmatched class target, else choice 0. Returns
    ``(builder, character, matched_race, matched_class)``.

    ``inject_race_if_unoffered`` appends a late race_hint SceneResult when no
    scene offered the target race — used ONLY by the world-tier negative test
    (proving an injected "Earthman" hint without barsoom world content does
    NOT conjure the boon).
    """
    from sidequest.game.builder import CharacterBuilder, FreeformInput, SceneResult
    from sidequest.genre.models import MechanicalEffects
    from sidequest.server.dispatch.char_creation_resolve import (
        resolve_char_creation_scenes,
    )

    scenes = resolve_char_creation_scenes(pack, world_slug=world_slug)
    assert scenes, f"no char_creation scenes resolved for world {world_slug!r}"

    builder = CharacterBuilder(
        scenes=scenes,
        rules=pack.rules,
        backstory_tables=pack.backstory_tables,
    ).with_lobby_name(name)
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    assert pack.classes, "heavy_metal must declare classes"
    builder = builder.with_classes(pack.classes)

    matched_race = race_display is None
    matched_class = class_display is None
    _guard = 0
    while not builder.is_confirmation():
        _guard += 1
        assert _guard < 60, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue

        idx = None
        if not matched_race:
            idx = next(
                (
                    i
                    for i, c in enumerate(scene.choices)
                    if c.mechanical_effects and c.mechanical_effects.race_hint == race_display
                ),
                None,
            )
            if idx is not None:
                matched_race = True
        if idx is None and not matched_class:
            idx = next(
                (
                    i
                    for i, c in enumerate(scene.choices)
                    if c.mechanical_effects and c.mechanical_effects.class_hint == class_display
                ),
                None,
            )
            if idx is not None:
                matched_class = True
        builder.apply_choice(idx if idx is not None else 0)

    if not matched_class and class_display is not None:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=class_display),
            )
        )
        matched_class = True
    if not matched_race and race_display is not None and inject_race_if_unoffered:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(race_hint=race_display),
            )
        )

    return builder, builder.build(name), matched_race, matched_class


def _race_source_leap_abilities(char):
    from sidequest.protocol.models import AbilitySource

    return [
        a
        for a in char.abilities
        if a.source == AbilitySource.Race and any(m in a.name.lower() for m in _LEAP_NAME_MARKERS)
    ]


def _native_race_display(pack) -> str:
    """A non-Earthman origin offered by the barsoom scenes (for negatives)."""
    from sidequest.server.dispatch.char_creation_resolve import (
        resolve_char_creation_scenes,
    )

    scenes = resolve_char_creation_scenes(pack, world_slug="barsoom")
    for scene in scenes:
        for choice in scene.choices or []:
            eff = choice.mechanical_effects
            if eff and eff.race_hint and eff.race_hint != "Earthman":
                return eff.race_hint
    pytest.fail("barsoom scenes offer no native origin (precondition)")


# ---------------------------------------------------------------------------
# Earthman gravity boon — STR edge + leap ability + OTEL (D5)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_earthman_build_carries_gravity_boon() -> None:
    """Earthman origin → race set, STR edge in the accumulator, leap ability
    with source=Race and an engine-facing mechanical_effect."""
    pack = _load_heavy_metal()
    builder, char, matched_race, _mc = _walk_and_build(
        pack, "John of Virginia", race_display="Earthman", class_display="Warrior"
    )

    assert matched_race, (
        "the barsoom chargen scenes must OFFER the Earthman origin as a choice "
        "(world-tier surface) — it was not found in any scene"
    )
    assert char.race == "Earthman", f"expected race 'Earthman'; got {char.race!r}"

    # STR edge: the world-authored stat_bonuses must land in the production
    # accumulator (generate_stats applies accumulated bonuses additively under
    # every stat-generation strategy — the pre-wired engine consumer).
    acc = builder.accumulated()
    assert acc.stat_bonuses.get("STR") == _EARTHMAN_STR_BONUS, (
        f"the Earthman choice's stat_bonuses must reach the accumulator "
        f"(STR == {_EARTHMAN_STR_BONUS}); got {acc.stat_bonuses!r}"
    )

    # Leap ability: source=Race, dual-voice, engine-facing effect text.
    leaps = _race_source_leap_abilities(char)
    assert len(leaps) == 1, (
        f"the Earthman must carry exactly one Race-source gravity/leap ability "
        f"(Keith: 'STR edge + leap ability'); got "
        f"{[(a.name, str(a.source)) for a in char.abilities]}"
    )
    boon = leaps[0]
    assert boon.genre_description.strip(), "leap ability needs a player-facing voice"
    assert boon.mechanical_effect.strip(), (
        "leap ability needs an engine-facing mechanical_effect — narrator-only "
        "flavor is exactly the unwired crunch this story forbids"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_earthman_boon_emits_origin_trait_otel_event() -> None:
    """Applying the gravity boon must emit chargen.origin_trait.applied with
    the origin and both consumers named — the GM panel is the lie detector."""
    pack = _load_heavy_metal()
    provider, exporter = _fresh_otel()
    tracer = provider.get_tracer("test_89_5_origin_trait")

    with tracer.start_as_current_span("chargen_walk"):
        _b, char, matched_race, _mc = _walk_and_build(
            pack, "Carter Figure", race_display="Earthman", class_display="Expert"
        )
    assert matched_race and char.race == "Earthman"

    events = _events_by_name(exporter)
    assert ORIGIN_TRAIT_EVENT in events, (
        f"{ORIGIN_TRAIT_EVENT} must fire when the boon is applied; saw events: {sorted(events)}"
    )
    attrs = events[ORIGIN_TRAIT_EVENT][0].attributes or {}
    assert attrs.get("origin") == "Earthman", (
        f"{ORIGIN_TRAIT_EVENT} must carry origin='Earthman'; got {dict(attrs)!r}"
    )
    assert "stat_bonuses" in attrs, (
        f"{ORIGIN_TRAIT_EVENT} must name the stat-edge consumer; got {dict(attrs)!r}"
    )
    assert "ability_names" in attrs, (
        f"{ORIGIN_TRAIT_EVENT} must name the granted abilities; got {dict(attrs)!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_native_build_gets_no_gravity_boon() -> None:
    """A native-origin barsoom build carries no boon: no Race-source leap
    ability, no STR bonus in the accumulator, no origin-trait event."""
    pack = _load_heavy_metal()
    native = _native_race_display(pack)

    provider, exporter = _fresh_otel()
    tracer = provider.get_tracer("test_89_5_native")
    with tracer.start_as_current_span("chargen_walk"):
        builder, char, matched_race, _mc = _walk_and_build(
            pack, "Tars of Thark", race_display=native, class_display="Warrior"
        )

    assert matched_race and char.race == native
    assert not _race_source_leap_abilities(char), (
        f"native origin {native!r} must NOT carry the gravity/leap ability "
        f"(four arms / native physiology is fiction-only)"
    )
    assert "STR" not in builder.accumulated().stat_bonuses, (
        f"native origin {native!r} must NOT carry the Earthman STR edge"
    )
    assert ORIGIN_TRAIT_EVENT not in _events_by_name(exporter), (
        f"{ORIGIN_TRAIT_EVENT} must not fire for a native origin"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_earthman_boon_is_world_tier_not_engine_hardcode() -> None:
    """Injecting race_hint='Earthman' into a non-barsoom WORLD build must NOT
    conjure the boon: the trait definition lives in barsoom world content. A
    global `if race == "Earthman"` engine hardcode would fail here.

    World choice (2026-06-08): walk ``evropi`` — a real heavy_metal world whose
    char_creation genuinely lacks the Earthman origin — rather than
    ``world_slug=None``. The loader's genre/world boundary correction
    (``loader.py`` ~1931) now builds the pack-level ``char_creation`` as the
    UNION of every world's scenes when the genre ships no genre-tier
    char_creation.yaml (heavy_metal moved chargen to the world tier), so
    ``world_slug=None`` aggregates barsoom and DOES offer the Earthman origin.
    evropi is the faithful "a heavy_metal build that doesn't offer the origin"
    crucible; injecting the bare race string there proves the boon comes from
    barsoom content, not a race-name hardcode in the engine."""
    pack = _load_heavy_metal()
    provider, exporter = _fresh_otel()
    tracer = provider.get_tracer("test_89_5_tier")

    with tracer.start_as_current_span("chargen_walk"):
        builder, char, _mr, _mc = _walk_and_build(
            pack,
            "Stranded Elsewhere",
            world_slug="evropi",  # real non-barsoom world; no Earthman origin offered
            race_display="Earthman",
            class_display="Warrior",
            inject_race_if_unoffered=True,
        )

    assert char.race == "Earthman", "precondition: injected race must stick"
    assert not _race_source_leap_abilities(char), (
        "a non-barsoom-world build with race 'Earthman' must NOT receive the "
        "gravity boon — the trait is barsoom WORLD content, not an engine hardcode"
    )
    assert "STR" not in builder.accumulated().stat_bonuses, (
        "no barsoom origin selected → no stat edge"
    )
    assert ORIGIN_TRAIT_EVENT not in _events_by_name(exporter), (
        f"{ORIGIN_TRAIT_EVENT} must not fire outside barsoom"
    )


# ---------------------------------------------------------------------------
# Barsoom caster Callings through the WORLD chargen surface
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("class_display", ["Mentalist", "Super-scientist"])
def test_barsoom_caster_seeds_populated_spellcasting(class_display: str) -> None:
    """A Barsoom caster built through the WORLD scenes seeds an Effort pool
    and a populated SpellcastingState from the 89-4 starting_prepared seeds.
    (The full Effort-max formula is asserted by the genre-tier sibling in
    test_wwn_heavy_metal_chargen.py — this proves the WORLD surface path.)"""
    pack = _load_heavy_metal()
    _b, char, _mr, matched_class = _walk_and_build(
        pack, f"{class_display} of Barsoom", class_display=class_display
    )

    assert matched_class, (
        f"the barsoom chargen scenes must OFFER {class_display!r} as a class "
        f"choice — injection fallback means the world surface does not offer it"
    )
    assert char.char_class == class_display

    cls = next(c for c in pack.classes if c.display_name == class_display)
    wm = cls.wwn_magic
    assert wm is not None, f"{class_display} must carry wwn_magic (full Calling)"

    src = wm.effort_sources[0].source
    assert src in char.core.effort, (
        f"{class_display} must seed effort[{src!r}]; got {list(char.core.effort)}"
    )

    sc = char.core.spellcasting
    assert sc is not None, f"{class_display} must seed a populated SpellcastingState"
    assert sc.casts_remaining == sc.casts_per_day == wm.casts_per_day_by_level["1"]
    capacity = wm.prepared_by_level.get("1", len(wm.starting_prepared))
    assert sc.prepared == wm.starting_prepared[:capacity]
    assert sc.prepared, f"{class_display} must start with prepared spells"
