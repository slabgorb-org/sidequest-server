"""Story 153-33 — WIRING proof: a real space_opera SWN character built through the
REAL ``CharacterBuilder.build()`` (with the production chargen-defs seam) is
granted background skills AND foci, per WWN/SWN SRD §1.3 (backgrounds →
free_skill + quick_skills) and §1.5 (foci → first-level skills + signature
ability).

This completes the deferred AC-2 of story 153-4. 153-4 fixed the ATTRIBUTE
spread; this story authors the missing space_opera content
(``backgrounds.yaml`` / ``foci.yaml`` / ``skills.yaml`` + the ``focus_id`` scene
references) so ``contribute_background_skills()`` and ``contribute_foci()`` —
both live and proven against synthetic fixtures in
``tests/game/test_chargen_seam_wiring.py`` — finally run with REAL inputs.

THE FINDING (epic-153 playtest sweep): space_opera ships no backgrounds/foci/
skills catalogs, so every SWN chargen resolves an EMPTY background catalog and
NO focus ids. ``contribute_background_skills`` fires with
``reason=no_matching_background_def`` and empty skills; ``contribute_foci``
fires with an empty focus list. A built character carries zero
background/focus-granted skills and an empty ``foci`` list. The narrator can
*describe* a Void-born pilot's training, but nothing mechanical backs it.

These tests drive the REAL production wiring — they mirror ``connect.py``'s
``resolve_backgrounds`` / ``resolve_foci`` / ``with_chargen_defs`` seam (the
153-4 spread test deliberately does NOT call ``with_chargen_defs``, so it never
exercised this path). Parametrized across all three LIVE space_opera worlds
(aureate_span, coyote_star, perseus_cloud) so the fix cannot be a half-wire that
covers one world and leaves the others mechanically mute.

RED until the Dev authors the content. Skips cleanly when sidequest-content is
not present on disk.

OTEL assertions use the monkeypatched-tracer pattern from
``tests/game/test_chargen_seam_wiring.py``; the span names are
``swn.chargen.background_skills`` and ``swn.chargen.foci_applied`` — the
slug-honesty invariant (space_opera binds ``swn``).
"""

from __future__ import annotations

import json
import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Every LIVE space_opera world. All three ship a char_creation.yaml whose
# `origins` scene tags a `background:` (Core-educated, Void-born, …). The fix
# must grant skills/foci in EVERY one of them — a one-world fix is a half-wire
# (CLAUDE.md: "No half-wired features").
SPACE_OPERA_WORLDS = ["aureate_span", "coyote_star", "perseus_cloud"]

# The canonical WWN/SWN SRD standard array — the 153-4 "14-to-7 spread". Pinned
# here as the AC-5 regression guard: authoring chargen content must not regress
# the attribute spread 153-4 landed.
WN_SHAPED_SPREAD = [14, 12, 11, 10, 9, 7]


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_space_opera():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route ``Span.open`` through an in-memory exporter for this test.

    Same fixture shape as tests/game/test_chargen_seam_wiring.py — the chargen
    contribution spans go through ``spans_module.tracer()``.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.153_33_swn_chargen_backgrounds_foci")
    monkeypatch.setattr(spans_module, "tracer", lambda: tracer)
    return exporter


def _build_first_choice_character(pack, world_slug: str, name: str):
    """Walk the real char_creation scenes picking choice 0, build, return char.

    Mirrors the PRODUCTION connect.py chargen seam: resolves scenes, classes,
    backgrounds, and foci world-first and attaches the background/focus catalogs
    via ``with_chargen_defs`` — the step the 153-4 spread test omits. Choice-0 /
    auto-advance / followup reaches confirmation for every space_opera world
    (no LLM-blocking scene), exactly like test_153_4_swn_chargen_spread_wiring.
    """
    from sidequest.game.builder import CharacterBuilder
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes
    from sidequest.server.dispatch.chargen_defs_resolve import (
        resolve_backgrounds,
        resolve_foci,
    )
    from sidequest.server.dispatch.class_resolve import resolve_classes

    scenes = resolve_char_creation_scenes(pack, world_slug=world_slug)
    assert scenes, f"space_opera/{world_slug} must declare char_creation scenes"

    builder = (
        CharacterBuilder(
            scenes=scenes,
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
            rng=random.Random(153_33),
        )
        .with_lobby_name(name)
        .with_classes(resolve_classes(pack, world_slug))
        .with_chargen_defs(
            backgrounds=resolve_backgrounds(pack, world_slug),
            foci=resolve_foci(pack, world_slug),
        )
    )
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)

    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 50, "chargen walk did not reach confirmation"
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
        builder.apply_choice(0)

    return builder.build(name)


def _span_by_name(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert matches, (
        f"expected span {name!r} to fire; got {[s.name for s in exporter.get_finished_spans()]}"
    )
    # The most-recent matching span (one build → one emit).
    return matches[-1]


# ---------------------------------------------------------------------------
# AC-1 / AC-4 — background skills land + the lie-detector span carries them
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("world_slug", SPACE_OPERA_WORLDS)
def test_real_swn_chargen_grants_background_skills(
    span_exporter: InMemorySpanExporter, world_slug: str
) -> None:
    """A real space_opera character is granted its background's free_skill +
    quick_skills (WWN SRD §1.3), and the ``swn.chargen.background_skills`` span
    carries a NON-EMPTY skill grant that matches what landed on the character.

    RED today: space_opera ships no backgrounds catalog, so the background tag
    (e.g. 'Core-educated') resolves to no def — the span fires with empty skills
    and ``reason=no_matching_background_def`` and nothing reaches the sheet.
    """
    pack = _load_space_opera()
    assert pack.rules.ruleset == "swn", f"space_opera must bind swn; got {pack.rules.ruleset!r}"

    char = _build_first_choice_character(pack, world_slug, "Kael Voss")

    bg_span = _span_by_name(span_exporter, "swn.chargen.background_skills")
    granted = json.loads(bg_span.attributes["skills"])
    assert granted, (
        f"[{world_slug}] background granted NO skills — the background catalog is "
        f"empty or the chosen background tag did not resolve. Span attrs: "
        f"{dict(bg_span.attributes)}"
    )

    # Max-of merge semantics (builder.py): a background grants its skills at a
    # FLOOR level (§1.3 → level 0); a focus or scene grant for the SAME skill can
    # raise it (never lower it). So assert the background skill landed at AT LEAST
    # its granted level, not exactly — mirroring the foci test below. (Concrete
    # case: a perseus_cloud "Regency-raised" + Officer build grants Lead from both
    # the background (0) and the chain-of-command focus (1) → sheet shows Lead 1.)
    for skill, level in granted.items():
        assert char.skills.get(skill, -1) >= level, (
            f"[{world_slug}] background skill {skill!r} (granted at level {level}) did not "
            f"land on the character sheet; char.skills={char.skills}"
        )


# ---------------------------------------------------------------------------
# AC-2 / AC-4 — foci land (skills + a focus id) + the foci_applied span
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("world_slug", SPACE_OPERA_WORLDS)
def test_real_swn_chargen_grants_foci(span_exporter: InMemorySpanExporter, world_slug: str) -> None:
    """A real space_opera character picks up at least one focus (WWN SRD §1.5):
    the focus id lands on ``Character.foci``, the focus's level-1 skills land on
    ``Character.skills``, and the ``swn.chargen.foci_applied`` span carries the
    non-empty focus list.

    RED today: no space_opera char_creation scene sets ``focus_id``, so no foci
    accumulate — the span fires with an empty focus list and the character's
    ``foci`` is empty. The Dev must author foci.yaml AND wire ``focus_id`` into
    the scene choices for every world.
    """
    pack = _load_space_opera()

    char = _build_first_choice_character(pack, world_slug, "Kael Voss")

    foci_span = _span_by_name(span_exporter, "swn.chargen.foci_applied")
    applied_foci = json.loads(foci_span.attributes["foci"])
    assert applied_foci, (
        f"[{world_slug}] chargen applied NO foci — no scene set a focus_id or the "
        f"foci catalog is empty. Span attrs: {dict(foci_span.attributes)}"
    )

    assert char.foci, (
        f"[{world_slug}] Character.foci is empty; the chosen focus id never reached "
        f"the sheet. Built foci={char.foci}"
    )

    granted_skills = json.loads(foci_span.attributes["skills"])
    assert granted_skills, (
        f"[{world_slug}] foci granted no skills — every authored space_opera focus's "
        f"first level should grant at least one skill (WWN SRD §1.5). "
        f"Span attrs: {dict(foci_span.attributes)}"
    )
    for skill, level in granted_skills.items():
        assert char.skills.get(skill, -1) >= level, (
            f"[{world_slug}] focus skill {skill!r}>={level} did not land (max-of merge); "
            f"char.skills={char.skills}"
        )


# ---------------------------------------------------------------------------
# AC-2 — a focus carries a signature ability (not just skills)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_real_swn_focus_grants_signature_ability(span_exporter: InMemorySpanExporter) -> None:
    """At least one chargen focus contributes a signature ability to the sheet
    (WWN SRD §1.5; ADR-095 — focus abilities are distinct from class abilities).

    Foci abilities are stamped ``AbilitySource.Class`` by the builder (there is
    no AbilitySource.Focus). The character must therefore carry MORE abilities
    than its class alone grants — proof a focus ability was applied, not just a
    focus skill. RED today: no foci are applied at all.
    """
    from sidequest.game.ability import AbilitySource

    pack = _load_space_opera()
    char = _build_first_choice_character(pack, "aureate_span", "Kael Voss")

    assert char.foci, f"no foci applied, so no focus ability can exist; char.foci={char.foci}"

    class_def = next((c for c in pack.classes if c.display_name == char.char_class), None)
    assert class_def is not None, f"built class {char.char_class!r} not in pack roster"
    class_ability_names = {a.name for a in (class_def.abilities or [])}

    focus_abilities = [
        a
        for a in char.abilities
        if a.source == AbilitySource.Class and a.name not in class_ability_names
    ]
    assert focus_abilities, (
        f"expected at least one focus-contributed ability beyond the class's own "
        f"({sorted(class_ability_names)}); character abilities="
        f"{[a.name for a in char.abilities]}"
    )


# ---------------------------------------------------------------------------
# AC-5 — the 153-4 shaped attribute spread is preserved after content authoring
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("world_slug", SPACE_OPERA_WORLDS)
def test_swn_chargen_still_shaped_spread_with_content(world_slug: str) -> None:
    """Authoring backgrounds/foci/skills (and editing scenes to add ``focus_id``)
    must NOT regress the 153-4 WN 14-to-7 attribute spread. A regression guard:
    the same built character that now carries skills+foci still has the shaped
    array, not flat point-buy.
    """
    pack = _load_space_opera()
    char = _build_first_choice_character(pack, world_slug, "Kael Voss")

    assert sorted(char.stats.values(), reverse=True) == WN_SHAPED_SPREAD, (
        f"[{world_slug}] the WN 14-to-7 spread regressed while authoring chargen "
        f"content; got {char.stats}"
    )
