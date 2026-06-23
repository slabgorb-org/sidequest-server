"""RED (story 126-24, AC7/AC9 e2e): the narrative chargen blank-Fate-sheet repro,
retired against the REAL pulp_noir / annees_folles content.

Forensic save 2026-06-19-annees_folles-cd25d503: walking the narrative wizard
(Origin=The Service / Signature=I Find Things Out / Connection=A Ghost / Drive=Answers)
presented an EMPTY Fate sheet — ``fate_pyramid`` fully blank, ``fate_aspects`` generic
placeholders, and every committed aspect had ``source_gear=null`` (the pack's signature
gear never compiled). This is the wiring proof (CLAUDE.md: "Verify Wiring, Not Just
Existence") that the genre seed table + gear seam reach the production
``CharacterBuilder`` end-to-end on the shipped pack.

Skips cleanly when sidequest-content is not on disk in this checkout.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

WORLD_SLUG = "annees_folles"
# The canonical Detective path from the story (crucible "I Find Things Out" -> Detective).
SIGNATURE_LABEL = "I Find Things Out"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_pulp_noir():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("pulp_noir"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _new_builder(pack):
    from sidequest.game.builder import CharacterBuilder
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=WORLD_SLUG)
    assert scenes, f"no char_creation scenes resolved for world {WORLD_SLUG!r}"
    builder = CharacterBuilder(
        scenes=scenes, rules=pack.rules, backstory_tables=pack.backstory_tables
    ).with_lobby_name("Camille Roux")
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    if pack.classes:
        builder = builder.with_classes(pack.classes)
    return builder


def _walk_capturing_seed(builder):
    """Walk the real narrative chargen, ACCEPTING the seed at each Fate step (the
    on-ramp player who takes the offered defaults). Returns the captured
    ``(seeded_allocation, seeded_free_aspects)`` presented at the Fate steps."""
    name = "Camille Roux"
    seeded_allocation: dict[str, int] = {}
    seeded_free_aspects: list[str] = []
    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 80, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        eff = scene.mechanical_effects
        step = eff.fate_chargen_step if eff is not None else None

        if step == "aspects":
            payload = builder.to_scene_message("p1").payload
            slots = payload.fate_aspect_slots or []
            seeded_free_aspects = [
                s.value
                for s in slots
                if s.kind not in ("high_concept", "trouble") and (s.value or "").strip()
            ]
            # Author HC/Trouble (no-silent-default) and ACCEPT the seeded free aspects.
            builder.apply_fate_aspects(
                high_concept="A Lawyer Who Lost the Bar but Kept the Brief",
                trouble="I Can't Leave a Loose Thread Alone",
                free_aspects=seeded_free_aspects,
            )
            continue
        if step == "pyramid":
            payload = builder.to_scene_message("p1").payload
            seeded_allocation = dict(payload.fate_current_allocation or {})
            # ACCEPT the seeded allocation as-is (the editable default, untouched).
            builder.apply_fate_pyramid(seeded_allocation)
            continue
        if step == "stunts":
            builder.apply_fate_stunts([])
            continue

        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue

        # Prefer the signature Detective choice, else any class_hint-bearing choice, else 0.
        idx = next(
            (i for i, c in enumerate(scene.choices) if c.label == SIGNATURE_LABEL),
            None,
        )
        if idx is None:
            idx = next(
                (
                    i
                    for i, c in enumerate(scene.choices)
                    if c.mechanical_effects and c.mechanical_effects.class_hint
                ),
                None,
            )
        builder.apply_choice(idx if idx is not None else 0)

    return seeded_allocation, seeded_free_aspects


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
class TestAnneesFollesNarrativeChargenSeed:
    def test_pyramid_presents_seeded_legal_allocation_not_blank(self) -> None:
        """AC7: the marquee repro — no blank sheet. The Fate pyramid step presents a
        non-empty, LEGAL pre-ranked allocation seeded from the narrative answers."""
        from sidequest.game.ruleset.fate_chargen import pyramid_violations
        from sidequest.genre.models.rules import FateConfig

        pack = _load_pulp_noir()
        builder = _new_builder(pack)
        seeded_allocation, _ = _walk_capturing_seed(builder)

        cfg = pack.rules.ruleset_config()
        assert isinstance(cfg, FateConfig)
        assert seeded_allocation, "annees_folles narrative chargen presented a BLANK pyramid"
        assert pyramid_violations(seeded_allocation, cfg) == []

    def test_aspects_step_presents_seeded_free_aspects(self) -> None:
        """AC7: the aspects step pre-fills at least one free aspect from the answers."""
        pack = _load_pulp_noir()
        builder = _new_builder(pack)
        _, seeded_free_aspects = _walk_capturing_seed(builder)
        assert seeded_free_aspects, "annees_folles narrative chargen presented EMPTY aspect slots"

    def test_final_sheet_carries_pack_gear_derived_aspects(self) -> None:
        """AC9: the pack's signature gear (rules.yaml ``fate.gear``) compiles onto the
        sheet via the narrative-wizard path — aspects carry ``source_gear`` (the stored
        save showed source_gear=null for all)."""
        pack = _load_pulp_noir()
        builder = _new_builder(pack)
        _walk_capturing_seed(builder)
        character = builder.build("Camille Roux")
        sheet = character.core.fate_sheet
        assert sheet is not None
        gear_aspects = [a for a in sheet.aspects if a.source_gear]
        assert gear_aspects, (
            "no gear-derived aspects on the sheet — the narrative-wizard path bypassed compile_gear_onto_sheet"
        )
