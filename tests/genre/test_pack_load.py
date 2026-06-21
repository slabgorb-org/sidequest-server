"""Integration test: caverns_and_claudes pack loads under dual-dial momentum schema.

Verifies that the migrated rules.yaml round-trips through GenrePack validation with
the dual-dial ConfrontationDef schema (player_metric + opponent_metric, BeatDef.kind).

See Task 27 — canary migration for dual-track momentum Phase 3.
"""

from __future__ import annotations

import pytest

from sidequest.genre.error import PackError
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, find_pack_path

CONTENT_ROOT = GENRE_PACKS_DIR
CC_PACK_DIR = CONTENT_ROOT / "caverns_and_claudes"


def _has_real_content() -> bool:
    return CC_PACK_DIR.is_dir()


def load_pack(slug: str) -> GenrePack:
    """Load a genre pack by slug from the sidequest-content tree."""
    return load_genre_pack(find_pack_path(slug))


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_caverns_and_claudes_pack_loads_with_wwn_combat_and_dial_schema():
    """caverns_and_claudes is bound ruleset: wwn (2026-06-12 port): its
    ``combat`` ("Dungeon Combat") moved off opposed_check dual-dial metrics to
    ``resolution_mode: beat_selection`` + ``win_condition: hp_depletion`` —
    combat resolves on HP reaching 0 and legitimately carries NO
    player_metric/opponent_metric, mirroring elemental_harmony / heavy_metal.
    The dual-dial invariant therefore only applies to its remaining dial
    confrontations (chase "Corridor Pursuit", negotiation), which are
    ``win_condition: dial_threshold`` (the model default) and still carry
    metrics. Filter on win_condition so the metricless hp_depletion combat is
    skipped rather than NPE-ing on ``player_metric.threshold``. At least one
    dial confrontation must remain so this assertion does not pass vacuously."""
    pack = load_pack("caverns_and_claudes")
    assert pack.rules is not None
    # Positive WWN-combat guard: COMBAT_PACKS in test_confrontation_calibration
    # is now empty, so this is the assertion that fails loudly if the combat
    # def is ever deleted or falls off the wwn binding.
    assert pack.rules.ruleset == "wwn"
    combat_defs = [c for c in pack.rules.confrontations if c.confrontation_type == "combat"]
    assert combat_defs, "caverns_and_claudes must expose a combat confrontation"
    for cdef in combat_defs:
        mode = (
            cdef.resolution_mode.value
            if hasattr(cdef.resolution_mode, "value")
            else cdef.resolution_mode
        )
        win = (
            cdef.win_condition.value if hasattr(cdef.win_condition, "value") else cdef.win_condition
        )
        assert mode == "beat_selection"
        assert win == "hp_depletion"
    dial_confrontations = [
        cdef
        for cdef in pack.rules.confrontations
        if (
            cdef.win_condition.value if hasattr(cdef.win_condition, "value") else cdef.win_condition
        )
        == "dial_threshold"
    ]
    assert dial_confrontations, (
        "caverns_and_claudes must retain at least one dial_threshold confrontation "
        "(chase/negotiation) for this dual-dial assertion to be meaningful"
    )
    for cdef in dial_confrontations:
        assert cdef.player_metric.threshold > 0
        assert cdef.opponent_metric.threshold > 0
        for beat in cdef.beats:
            kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
            assert kind in {"strike", "brace", "push", "angle"}


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_pack_loads_with_dual_dial_schema():
    """heavy_metal is bound ruleset: wwn (Story 1): its ``combat`` ("Blade-work")
    moved off opposed_check dual-dial metrics to ``resolution_mode:
    beat_selection`` + ``win_condition: hp_depletion`` — combat resolves on HP
    reaching 0 and legitimately carries NO player_metric/opponent_metric,
    mirroring elemental_harmony / space_opera. The dual-dial invariant therefore
    only applies to its remaining dial confrontations (negotiation, chase),
    which are ``win_condition: dial_threshold``
    (the model default) and still carry metrics. Filter on win_condition so the
    metricless hp_depletion combat is skipped rather than NPE-ing on
    ``player_metric.threshold``. At least one dial confrontation must remain so
    this assertion does not pass vacuously."""
    pack = load_pack("heavy_metal")
    assert pack.rules is not None
    dial_confrontations = [
        cdef
        for cdef in pack.rules.confrontations
        if (
            cdef.win_condition.value if hasattr(cdef.win_condition, "value") else cdef.win_condition
        )
        == "dial_threshold"
    ]
    assert dial_confrontations, (
        "heavy_metal must retain at least one dial_threshold confrontation "
        "(negotiation/chase) for this dual-dial assertion to be meaningful"
    )
    for cdef in dial_confrontations:
        assert cdef.player_metric.threshold > 0
        assert cdef.opponent_metric.threshold > 0
        for beat in cdef.beats:
            kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
            assert kind in {"strike", "brace", "push", "angle"}


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_space_opera_pack_loads_with_dual_dial_schema():
    """space_opera is the SWN-bound pack (Task 8 of the space_opera→SWN binding):
    its ``combat`` and ``ship_combat`` confrontations moved off opposed_check
    dual-dial metrics to ``resolution_mode: beat_selection`` +
    ``win_condition: hp_depletion`` — combat now resolves on HP reaching 0 and
    legitimately carries NO player_metric/opponent_metric. The dual-dial
    invariant therefore only applies to its remaining dial confrontations
    (negotiation, chase, dogfight), which are ``win_condition: dial_threshold``
    (the model default) and still carry metrics. Filter on win_condition so the
    metricless hp_depletion confrontations are skipped rather than NPE-ing on
    ``player_metric.threshold``. At least one dial confrontation must remain so
    this assertion does not pass vacuously."""
    pack = load_pack("space_opera")
    assert pack.rules is not None
    dial_confrontations = [
        cdef
        for cdef in pack.rules.confrontations
        if (
            cdef.win_condition.value if hasattr(cdef.win_condition, "value") else cdef.win_condition
        )
        == "dial_threshold"
    ]
    assert dial_confrontations, (
        "space_opera must retain at least one dial_threshold confrontation "
        "(negotiation/chase/dogfight) for this dual-dial assertion to be meaningful"
    )
    for cdef in dial_confrontations:
        assert cdef.player_metric.threshold > 0
        assert cdef.opponent_metric.threshold > 0
        for beat in cdef.beats:
            kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
            assert kind in {"strike", "brace", "push", "angle"}


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_spaghetti_western_pack_loads_as_fully_fate():
    """Story 153-3: spaghetti_western is a Fate-bound pack, so it no longer carries
    ANY native dial confrontation — the dual-dial schema (asserted for the WN-family
    packs above) does not apply. Every confrontation resolves through a Fate mode:
    standoff/negotiation/chase -> contest, combat -> conflict (lethal), poker ->
    table_resolution. The loud Fate-mode guard (ADR-144) would reject the pack at
    load if any native beat_selection/opposed_check def survived, so a clean load
    AND an all-Fate-mode set are the assertion."""
    pack = load_pack("spaghetti_western")
    assert pack.rules is not None
    assert pack.rules.ruleset == "fate"
    fate_modes = {"contest", "conflict", "table_resolution", "sealed_letter_lookup"}
    modes = {
        cdef.confrontation_type: (
            cdef.resolution_mode.value
            if hasattr(cdef.resolution_mode, "value")
            else cdef.resolution_mode
        )
        for cdef in pack.rules.confrontations
    }
    assert modes, "spaghetti_western declares no confrontations"
    for ctype, mode in modes.items():
        assert mode in fate_modes, f"spaghetti_western {ctype!r} uses non-Fate mode {mode!r}"
    assert modes.get("combat") == "conflict"  # a gunfight is lethal — a Conflict, not a Contest


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_mutant_wasteland_pack_loads_with_dual_dial_schema():
    pack = load_pack("mutant_wasteland")
    assert pack.rules is not None
    for cdef in pack.rules.confrontations:
        assert cdef.player_metric.threshold > 0
        assert cdef.opponent_metric.threshold > 0
        for beat in cdef.beats:
            kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
            assert kind in {"strike", "brace", "push", "angle"}


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_elemental_harmony_pack_loads_with_dual_dial_schema():
    """elemental_harmony is the WWN-bound pack: its ``combat`` confrontation
    ("Martial Exchange") moved off opposed_check dual-dial metrics to
    ``resolution_mode: beat_selection`` + ``win_condition: hp_depletion`` —
    combat now resolves on HP reaching 0 and legitimately carries NO
    player_metric/opponent_metric. The dual-dial invariant therefore only
    applies to its remaining dial confrontations (negotiation "Diplomatic
    Council", chase "Pursuit"), which are ``win_condition: dial_threshold``
    (the model default) and still carry metrics. Filter on win_condition so
    the metricless hp_depletion confrontations are skipped rather than
    NPE-ing on ``player_metric.threshold``. At least one dial confrontation
    must remain so this assertion does not pass vacuously."""
    pack = load_pack("elemental_harmony")
    assert pack.rules is not None
    dial_confrontations = [
        cdef
        for cdef in pack.rules.confrontations
        if (
            cdef.win_condition.value if hasattr(cdef.win_condition, "value") else cdef.win_condition
        )
        == "dial_threshold"
    ]
    assert dial_confrontations, (
        "elemental_harmony must retain at least one dial_threshold confrontation "
        "(negotiation/chase) for this dual-dial assertion to be meaningful"
    )
    for cdef in dial_confrontations:
        assert cdef.player_metric.threshold > 0
        assert cdef.opponent_metric.threshold > 0
        for beat in cdef.beats:
            kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
            assert kind in {"strike", "brace", "push", "angle"}


# ---------------------------------------------------------------------------
# Task 5: class_filter / encounter_beat_choices cross-reference validation
# ---------------------------------------------------------------------------


def test_pack_load_rejects_dangling_class_filter(tmp_path, minimal_pack_factory):
    """class_filter must reference a class declared in classes.yaml."""
    pack = minimal_pack_factory(tmp_path)
    pack.set_rules_yaml(
        confrontations=[
            {
                "type": "combat",
                "label": "C",
                "category": "combat",
                "player_metric": {"name": "m", "starting": 0, "threshold": 7},
                "opponent_metric": {"name": "m", "starting": 0, "threshold": 7},
                "beats": [
                    {
                        "id": "ghost_strike",
                        "label": "X",
                        "kind": "strike",
                        "stat_check": "STR",
                        "class_filter": ["Necromancer"],  # not in classes.yaml
                    }
                ],
            }
        ],
        allowed_classes=["Fighter"],
    )
    pack.set_classes_yaml(
        [
            {
                "id": "fighter",
                "display_name": "Fighter",
                "rpg_role": "tank",
                "jungian_default": "hero",
                "prime_requisite": "STR",
                "minimum_score": 9,
                "kit_table": "fighter_kit",
                "flavor": "—",
                "encounter_beat_choices": ["ghost_strike"],
            }
        ]
    )
    with pytest.raises(PackError, match="class_filter.*Necromancer.*not declared"):
        load_genre_pack(pack.path)


def test_pack_load_rejects_dangling_encounter_beat_choice(tmp_path, minimal_pack_factory):
    """encounter_beat_choices must reference a beat that exists in some pool."""
    pack = minimal_pack_factory(tmp_path)
    pack.set_rules_yaml(
        confrontations=[
            {
                "type": "combat",
                "label": "C",
                "category": "combat",
                "player_metric": {"name": "m", "starting": 0, "threshold": 7},
                "opponent_metric": {"name": "m", "starting": 0, "threshold": 7},
                "beats": [{"id": "attack", "label": "A", "kind": "strike", "stat_check": "STR"}],
            }
        ],
        allowed_classes=["Fighter"],
    )
    pack.set_classes_yaml(
        [
            {
                "id": "fighter",
                "display_name": "Fighter",
                "rpg_role": "tank",
                "jungian_default": "hero",
                "prime_requisite": "STR",
                "minimum_score": 9,
                "kit_table": "fighter_kit",
                "flavor": "—",
                "encounter_beat_choices": ["attack", "smite"],  # smite missing
            }
        ]
    )
    with pytest.raises(PackError, match="encounter_beat_choices.*smite.*not in pool"):
        load_genre_pack(pack.path)


def test_pack_load_rejects_empty_encounter_beat_choices_for_allowed_class(
    tmp_path, minimal_pack_factory
):
    """A class in allowed_classes must have a non-empty encounter_beat_choices.

    TODO(task-14): once Task 14 lands content with non-empty per-class beat lists,
    this validator and the C&C pack will both go green together.
    """
    pack = minimal_pack_factory(tmp_path)
    pack.set_rules_yaml(
        confrontations=[
            {
                "type": "combat",
                "label": "C",
                "category": "combat",
                "player_metric": {"name": "m", "starting": 0, "threshold": 7},
                "opponent_metric": {"name": "m", "starting": 0, "threshold": 7},
                "beats": [{"id": "attack", "label": "A", "kind": "strike", "stat_check": "STR"}],
            }
        ],
        allowed_classes=["Fighter"],
    )
    pack.set_classes_yaml(
        [
            {
                "id": "fighter",
                "display_name": "Fighter",
                "rpg_role": "tank",
                "jungian_default": "hero",
                "prime_requisite": "STR",
                "minimum_score": 9,
                "kit_table": "fighter_kit",
                "flavor": "—",
                "encounter_beat_choices": [],
            }
        ]
    )
    with pytest.raises(PackError, match="encounter_beat_choices.*empty"):
        load_genre_pack(pack.path)


def test_pack_load_accepts_valid_class_filter_and_beat_choices(tmp_path, minimal_pack_factory):
    """Validator passes silently when class_filter / encounter_beat_choices are well-formed.

    Guards against a regression where the validator accidentally raises on valid packs.
    """
    pack = minimal_pack_factory(tmp_path)
    pack.set_rules_yaml(
        confrontations=[
            {
                "type": "combat",
                "label": "C",
                "category": "combat",
                "player_metric": {"name": "m", "starting": 0, "threshold": 7},
                "opponent_metric": {"name": "m", "starting": 0, "threshold": 7},
                "beats": [
                    {"id": "attack", "label": "A", "kind": "strike", "stat_check": "STR"},
                    {
                        "id": "cleave",
                        "label": "C",
                        "kind": "strike",
                        "stat_check": "STR",
                        "class_filter": ["Fighter"],
                    },
                ],
            }
        ],
        allowed_classes=["Fighter"],
    )
    pack.set_classes_yaml(
        [
            {
                "id": "fighter",
                "display_name": "Fighter",
                "rpg_role": "tank",
                "jungian_default": "hero",
                "prime_requisite": "STR",
                "minimum_score": 9,
                "kit_table": "fighter_kit",
                "flavor": "—",
                "encounter_beat_choices": ["attack", "cleave"],
            }
        ]
    )
    # Should not raise — declared class, in-pool beat IDs, non-empty list
    pack_obj = load_genre_pack(pack.path)
    assert pack_obj is not None
    assert pack_obj.rules is not None
    assert pack_obj.rules.confrontations
    assert any(c.display_name == "Fighter" for c in pack_obj.classes)


# ---------------------------------------------------------------------------
# Task 8: saving_throws required on every class when pack has spell catalogs
# ---------------------------------------------------------------------------

# Minimal class dict shared by the saving-throws tests.  Encounter-beat
# choices deliberately empty because the minimal fixture rules.yaml uses
# magic_level="none" and no allowed_classes — so _validate_class_filter_refs
# does not run against these entries.
_BASE_CLASS: dict = {
    "id": "magic_user",
    "display_name": "Magic-User",
    "rpg_role": "blaster",
    "jungian_default": "magician",
    "prime_requisite": "INT",
    "minimum_score": 9,
    "kit_table": "mage_kit",
    "flavor": "Frail but deadly.",
    "encounter_beat_choices": [],
}

_SAVING_THROWS: dict = {
    "death_ray_or_poison": 13,
    "magic_wands": 14,
    "paralysis_or_stone": 13,
    "dragon_breath": 16,
    "rods_staves_spells": 15,
}


def test_pack_load_rejects_class_without_saving_throws_when_spell_catalog_present(
    tmp_path, minimal_pack_factory
):
    """A pack with a spells/ directory must have saving_throws on every class.

    This is the loudest guard against shipping a spell catalog without the
    B/X B26 save tables that spell effects require to resolve.
    """
    pack = minimal_pack_factory(tmp_path)
    pack.create_spells_dir()
    # Class intentionally missing saving_throws (the default None).
    pack.set_classes_yaml([dict(_BASE_CLASS)])  # no saving_throws key
    with pytest.raises(PackError, match="saving_throws"):
        load_genre_pack(pack.path)


def test_pack_load_accepts_all_classes_with_saving_throws_and_spell_catalog(
    tmp_path, minimal_pack_factory
):
    """When every class declares saving_throws, the validator passes even with a
    spells/ catalog present.
    """
    pack = minimal_pack_factory(tmp_path)
    pack.create_spells_dir()
    pack.set_classes_yaml([{**_BASE_CLASS, "saving_throws": _SAVING_THROWS}])
    # Should not raise.
    pack_obj = load_genre_pack(pack.path)
    assert pack_obj is not None
    assert pack_obj.classes
    assert pack_obj.classes[0].saving_throws is not None


def test_pack_load_skips_saving_throws_check_without_spell_catalog(tmp_path, minimal_pack_factory):
    """Packs without a spells/ directory do not require saving_throws on classes.

    This guards heavy_metal, tea_and_murder, etc. which have no magic system yet.
    """
    pack = minimal_pack_factory(tmp_path)
    # No create_spells_dir() call — no spells/ dir exists.
    pack.set_classes_yaml([dict(_BASE_CLASS)])  # no saving_throws
    # Should not raise — validator is no-op without spell catalogs.
    pack_obj = load_genre_pack(pack.path)
    assert pack_obj is not None
