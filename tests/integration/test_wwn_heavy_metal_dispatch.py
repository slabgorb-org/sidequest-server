"""heavy_metal → WWN Story 2 — cast_spell end-to-end wiring proof (real pack).

RED for story 87-2 (real magic, epic Story 3 folded in). Mirrors the mandated EH
proof (tests/integration/test_wwn_elemental_harmony_dispatch.py) on the real
heavy_metal pack: a real Mage-tradition caster casts a real damage spell into the
real Blade-work combat through the PRODUCTION apply path
(``_apply_narration_result_to_snapshot``), and we assert:

  1. the cast spent one cast (casts_remaining decremented);
  2. the opponent's HP was ablated through the HP channel;
  3. the ``wwn.spell.cast`` span fired with refused==False (the GM-panel lie detector);
  4. NEGATIVE — the B/X ``magic.cast_spell_*`` watcher events did NOT fire, proving
     the wwn arm took the branch (not the B/X innate-cast path).

The caster class + damage spell are DISCOVERED from the loaded pack (not hardcoded),
so the proof does not couple to the specific spell ids Dev authors.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# heavy_metal Blade-work opponent_default_stats (rules.yaml, Story 1).
_OPPONENT_HP = 10
_OPPONENT_AC = 12


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _discover_caster_and_damage_spell(pack):
    """Return (class_display, spell_id) for a caster whose starting_prepared holds
    a damage spell. RED-clear if none exists (Dev must give a caster a damage spell)."""
    cat = pack.wwn_spell_catalog
    assert cat is not None, "spells_wwn.yaml must be authored (real magic) — wwn_spell_catalog is None"
    damage_ids = {s.id for s in cat.spells if s.damage_die}
    assert damage_ids, "the spell catalog must include at least one damage spell"
    assert pack.classes is not None
    for cls in pack.classes:
        if cls.wwn_magic is None:
            continue
        for sid in cls.wwn_magic.starting_prepared:
            if sid in damage_ids:
                return cls.display_name, sid
    raise AssertionError(
        "no caster starts with a damage spell prepared — combat casting cannot ablate HP"
    )


def _build_caster(pack, name: str, *, class_display: str):
    from sidequest.game.builder import CharacterBuilder, FreeformInput, SceneResult
    from sidequest.genre.models import MechanicalEffects
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=None)
    assert scenes, "heavy_metal must declare char_creation scenes"

    builder = CharacterBuilder(
        scenes=scenes,
        rules=pack.rules,
        backstory_tables=pack.backstory_tables,
    ).with_lobby_name(name)
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    builder = builder.with_classes(pack.classes)

    matched = False
    _guard = 0
    while not builder.is_confirmation():
        _guard += 1
        assert _guard < 50, "chargen walk did not reach confirmation"
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
        idx = next(
            (
                i
                for i, c in enumerate(scene.choices)
                if c.mechanical_effects and c.mechanical_effects.class_hint == class_display
            ),
            None,
        )
        if idx is not None:
            matched = True
            builder.apply_choice(idx)
        else:
            builder.apply_choice(0)

    if not matched:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=class_display),
            )
        )
    return builder.build(name)


class _RecordingHub:
    """Records every watcher event for the B/X negative assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    def __call__(self, event_type, payload, *, component, **_kw) -> None:
        self.events.append((event_type, payload, component))


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_wwn_cast_spell_routes_through_wwn_module_on_real_heavy_metal(otel_capture, monkeypatch):
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = _load_heavy_metal()
    assert pack.rules.ruleset == "wwn", "heavy_metal must be bound ruleset: wwn"

    class_display, spell_id = _discover_caster_and_damage_spell(pack)

    # ── Build the real caster; it must seed the spell we will cast ─────────
    caster_name = "Sael"
    caster = _build_caster(pack, caster_name, class_display=class_display)
    assert caster.char_class == class_display, f"chargen must resolve {class_display}; got {caster.char_class!r}"
    sc = caster.core.spellcasting
    assert sc is not None, f"{class_display} must be seeded with a SpellcastingState"
    assert spell_id in sc.prepared, f"{class_display} must have {spell_id!r} prepared; got {sc.prepared!r}"
    casts_before = sc.casts_remaining
    assert casts_before >= 1, "caster must start with at least one cast"

    # ── Seat the real Blade-work combat via the production seam ────────────
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(caster)
    opponent = "The Collector's Blade"
    snap.character_locations[caster_name] = "The Antechamber"
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=caster_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc

    opponent_core = snap.find_creature_core(opponent)
    assert opponent_core is not None, "opponent core must be reachable to ablate"
    assert opponent_core.armor_class == _OPPONENT_AC, "opponent AC from opponent_default_stats"
    assert opponent_core.hp.current == _OPPONENT_HP, "opponent HP from opponent_default_stats"
    hp_before = opponent_core.hp.current

    # Pin rng MAX → defender save made (damage halved but still > 0, below kill).
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: b)
    hub = _RecordingHub()
    monkeypatch.setattr("sidequest.server.narration_apply._watcher_publish", hub)

    result = NarrationTurnResult(
        narration=f"{caster_name} looses the working.",
        beat_selections=[BeatSelection(actor=caster_name, beat_id="cast_spell", spell_id=spell_id)],
    )
    room = room_for(snap)
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name=caster_name,
        pack=pack,
        from_explicit_action=True,
        room=room,
        acting_character_name=caster_name,
    )

    # 1. one cast spent
    caster_after = snap.find_creature_core(caster_name)
    assert caster_after is not None and caster_after.spellcasting is not None
    assert caster_after.spellcasting.casts_remaining == casts_before - 1, (
        f"casting {spell_id!r} must spend exactly one cast through the real apply path; "
        f"before={casts_before} after={caster_after.spellcasting.casts_remaining}"
    )

    # 2. opponent HP ablated
    assert opponent_core.hp.current < hp_before, (
        f"{spell_id!r} damage must ablate the opponent's HP through the WWN cast spine; "
        f"before={hp_before} after={opponent_core.hp.current}"
    )

    # 3. wwn.spell.cast span fired (lie detector)
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(spans) >= 1, (
        f"the WWN cast spine must emit a wwn.spell.cast span on the real apply path; got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert spans[-1].attributes.get("refused") is False, (
        "a valid cast (prepared + a cast remaining) must record refused=False"
    )

    # 4. NEGATIVE — the B/X cast arm did not fire
    bx_events = [e for e in hub.events if e[0].startswith("magic.cast_spell")]
    assert not bx_events, (
        f"the B/X cast path (magic.cast_spell_*) must NOT fire on a ruleset: wwn pack; "
        f"got {bx_events!r}"
    )
