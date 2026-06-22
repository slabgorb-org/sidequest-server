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

THE FINDING (epic-153 playtest sweep) — the pre-153-33 state these tests guard
against: space_opera shipped no backgrounds/foci/skills catalogs, so every SWN
chargen resolved an EMPTY background catalog and NO focus ids.
``contribute_background_skills`` fired with ``reason=no_matching_background_def``
and empty skills; ``contribute_foci`` fired with an empty focus list. A built
character carried zero background/focus-granted skills and an empty ``foci``
list — the narrator could *describe* a Void-born pilot's training, but nothing
mechanical backed it. 153-33 authored the content; this suite is now the
regression guard that keeps it wired.

These tests drive the REAL production wiring — they mirror ``connect.py``'s
``resolve_backgrounds`` / ``resolve_foci`` / ``with_chargen_defs`` seam (the
153-4 spread test deliberately does NOT call ``with_chargen_defs``, so it never
exercised this path). Parametrized across all three LIVE space_opera worlds
(aureate_span, coyote_star, perseus_cloud) so the fix cannot be a half-wire that
covers one world and leaves the others mechanically mute.

GREEN since 153-33 authored the space_opera catalogs + ``focus_id`` wiring.
Skips cleanly when sidequest-content is not present on disk.

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


def _build_first_choice_character(pack, world_slug: str, name: str, *, crucible_choice: int = 0):
    """Walk the real char_creation scenes picking choice 0, build, return char.

    Mirrors the PRODUCTION connect.py chargen seam: resolves scenes, classes,
    backgrounds, and foci world-first and attaches the background/focus catalogs
    via ``with_chargen_defs`` — the step the 153-4 spread test omits. Choice-0 /
    auto-advance / followup reaches confirmation for every space_opera world
    (no LLM-blocking scene), exactly like test_153_4_swn_chargen_spread_wiring.

    ``crucible_choice`` selects which vocation (and therefore which focus) is
    taken at the ``crucible`` scene; every other choice-bearing scene still takes
    choice 0. Default 0 = the first vocation (Officer → chain-of-command focus).
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
            # A no-choice scene is either display-only (auto-advance) or wants a
            # freeform answer. Dispatch on the scene's own ``allows_freeform`` flag
            # rather than catching exceptions — apply_auto_advance() raises
            # InvalidChoiceError precisely when ``scene.allows_freeform`` is set, so
            # a bare ``except Exception`` here would also swallow a real WrongPhaseError
            # (an FSM bug this wiring test exists to surface). Let it propagate.
            if scene.allows_freeform:
                builder.apply_freeform(name)
            else:
                builder.apply_auto_advance()
            continue
        builder.apply_choice(crucible_choice if scene.id == "crucible" else 0)

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

    Regression guard: before 153-33, space_opera shipped no backgrounds catalog,
    so the background tag (e.g. 'Core-educated') resolved to no def — the span
    fired with empty skills and ``reason=no_matching_background_def`` and nothing
    reached the sheet. This now asserts the grant lands.
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

    Regression guard: before 153-33, no space_opera char_creation scene set
    ``focus_id``, so no foci accumulated — the span fired with an empty focus list
    and the character's ``foci`` was empty. 153-33 wired ``focus_id`` into every
    world's crucible choices; this now asserts the focus lands.
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


# (crucible choice index, expected focus id, expected signature-ability name).
# The crucible scene is identical across all three worlds (genre-tier foci), so
# each vocation choice grants exactly one focus + its level-1 signature ability.
# Walking all five choices proves EVERY focus's ability reaches the sheet — not
# just choice-0's (chain-of-command), which is all a single-walk test would cover.
_FOCI_BY_CRUCIBLE = [
    (0, "chain-of-command", "Pull Rank"),
    (1, "jury-rigger", "Make It Hold"),
    (2, "dead-reckoning", "Know the Lanes"),
    (3, "black-market-contacts", "Know a Guy"),
    (4, "cultural-fluency", "Read the Room"),
]


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("crucible_idx,focus_id,ability_name", _FOCI_BY_CRUCIBLE)
def test_real_swn_focus_grants_signature_ability(
    span_exporter: InMemorySpanExporter, crucible_idx: int, focus_id: str, ability_name: str
) -> None:
    """Each chargen focus contributes its signature ability to the sheet
    (WWN SRD §1.5; ADR-097 — focus abilities are distinct from class abilities).

    Foci abilities are stamped ``AbilitySource.Class`` by the builder (there is
    no AbilitySource.Focus). The character must therefore carry MORE abilities
    than its class alone grants — proof a focus ability was applied, not just a
    focus skill. Parametrized over all five crucible vocations so a focus authored
    skill-only (no ability) cannot slip through (``FocusLevel.abilities`` defaults
    to ``[]`` and the validator does not require an ability).
    """
    from sidequest.game.ability import AbilitySource

    pack = _load_space_opera()
    char = _build_first_choice_character(
        pack, "aureate_span", "Kael Voss", crucible_choice=crucible_idx
    )

    assert focus_id in char.foci, (
        f"crucible choice {crucible_idx} should grant focus {focus_id!r}; char.foci={char.foci}"
    )

    class_def = next((c for c in pack.classes if c.display_name == char.char_class), None)
    assert class_def is not None, f"built class {char.char_class!r} not in pack roster"
    class_ability_names = {a.name for a in (class_def.abilities or [])}

    # The focus's named signature ability is on the sheet, stamped Class-source,
    # and is NOT one of the class's own abilities (ADR-097 distinctness).
    assert ability_name not in class_ability_names, (
        f"focus ability {ability_name!r} collides with a class ability name "
        f"({sorted(class_ability_names)}) — ADR-097 requires them distinct"
    )
    match = next((a for a in char.abilities if a.name == ability_name), None)
    assert match is not None, (
        f"focus {focus_id!r} signature ability {ability_name!r} did not reach the sheet; "
        f"character abilities={[a.name for a in char.abilities]}"
    )
    assert match.source == AbilitySource.Class, (
        f"focus ability {ability_name!r} should be stamped AbilitySource.Class; got {match.source!r}"
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
