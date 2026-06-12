"""WWN caverns_and_claudes Task 8 — the MANDATED end-to-end wiring proof.

Task 7 wired ``cast_spell`` -> ``WwnRulesetModule.resolve_spellcast`` in
``narration_apply`` (``_resolve_wwn_cast_for_beat``, ruleset-gated) and proved it
with SYNTHETIC fixtures (``tests/server/test_wwn_cast_dispatch.py``). Synthetic
proof is necessary-but-not-sufficient: ``beat_selection`` / ``hp_depletion``
combat features carry a known path-trap where dispatch-only wiring can no-op in
real play if the test drives a synthetic fixture instead of the real pack's
combat path.

THIS test closes that trap. It drives the ACTUAL ``caverns_and_claudes`` pack:

  - a real ``Mage`` caster built through the real ``CharacterBuilder.build()``
    (so ``core.spellcasting`` is seeded from the class's ``starting_prepared``);
  - a real "Dungeon Combat" ``combat`` (``beat_selection`` / ``hp_depletion``)
    seated through the PRODUCTION ``instantiate_encounter_from_trigger`` seam
    (the opponent's runtime ``CreatureCore`` hp/AC come from the authored
    ``opponent_default_stats``);
  - a ``cast_spell`` BeatSelection driven through the REAL
    ``_apply_narration_result_to_snapshot`` apply path — the function Task 7
    gated at ~:3148 — exactly the way real play reaches it (NOT by calling
    ``_resolve_wwn_cast_for_beat`` directly, which is the synthetic shortcut
    Task 7 already covered).

The wiring proof asserts (all on the real pack, through the real apply path):

  1. ``caster.core.spellcasting.casts_remaining`` decremented (2 -> 1);
  2. the opponent's ``core.hp.current`` REDUCED (wracking_bolt damage applied
     through the HP channel);
  3. the ``wwn.spell.cast`` span fired (InMemorySpanExporter — the lie detector,
     survives refactor);
  4. NEGATIVE assertion — the B/X ``magic.cast_spell_*`` watcher events did NOT
     fire, proving the wwn arm took the branch (``_resolve_wwn_cast_for_beat``)
     and NOT the B/X ``_resolve_innate_cast_for_beat``. Doubly load-bearing —
     this port just retired the B/X innate path for this pack.

rng is pinned via monkeypatch so the save + damage rolls are deterministic.

Skips cleanly when sidequest-content is not present on disk (mirrors the sibling
integration tests' skip pattern).
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Authored opponent stats on Dungeon Combat's opponent_default_stats
# (rules.yaml: hp: 10, armor_class: 12). The dungeon mook.
_OPPONENT_HP = 10
_OPPONENT_AC = 12

# The Mage's authored starting_prepared (classes.yaml).
_EXPECTED_PREPARED = ["wracking_bolt", "sleepless_tithe"]
_DAMAGE_SPELL = "wracking_bolt"  # 1d6, damage_per_level, save: physical


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_caverns_and_claudes():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("caverns_and_claudes"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _build_mage(pack, name: str):
    """Build a real Mage from the real pack via CharacterBuilder.build().

    Walks the real char_creation scenes, selecting choice index 2 in the
    the_calling scene ("Mage", class_hint: Mage) so build() resolves the real
    Mage ClassDef and seed_wwn_magic seeds core.spellcasting from the class's
    starting_prepared. The remaining scenes are advanced generically
    (choice 0 / auto-advance / followup) until confirmation.
    """
    from sidequest.game.builder import CharacterBuilder
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=None)
    assert scenes, "caverns_and_claudes must declare char_creation scenes"

    builder = CharacterBuilder(
        scenes=scenes,
        rules=pack.rules,
        backstory_tables=pack.backstory_tables,
    ).with_lobby_name(name)
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    assert pack.classes, "caverns_and_claudes must declare classes (Mage)"
    builder = builder.with_classes(pack.classes)

    # Drive scenes to confirmation. the_calling choice 2 is the Mage;
    # every other scene is advanced generically.
    _guard = 0
    while not builder.is_confirmation():
        _guard += 1
        assert _guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup("Mage of the Ropefoot camp")
            continue
        scene = builder.current_scene()
        if not scene.choices:
            # display-only scene OR name-entry scene
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue
        if scene.id == "the_calling":
            builder.apply_choice(2)  # Mage
        else:
            builder.apply_choice(0)

    character = builder.build(name)
    return character


def _seat_dungeon_combat(pack, snap, *, caster_name: str, opponent: str, location: str):
    """Seat a real caverns_and_claudes Dungeon Combat via the PRODUCTION path.

    Uses ``instantiate_encounter_from_trigger`` so the opponent's CreatureCore
    is seeded with the authored hp/AC and reachable via find_creature_core —
    exactly as in live play. Returns the StructuredEncounter.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap.character_locations[caster_name] = location
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=caster_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="caverns_and_claudes",
    )
    assert enc is not None, "seating Dungeon Combat must produce an encounter"
    snap.encounter = enc
    return enc


class _RecordingHub:
    """Drop-in replacement for the watcher hub that records every event.

    Used for the B/X negative assertion: the B/X cast path
    (``_resolve_innate_cast_for_beat``) publishes ``magic.cast_spell_*`` events
    through ``_watcher_publish``; if the wwn arm takes the branch, NONE of those
    fire.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    def __call__(self, event_type, payload, *, component, **_kw) -> None:
        self.events.append((event_type, payload, component))


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_wwn_cast_spell_routes_through_wwn_module_on_real_caverns_and_claudes(
    otel_capture, monkeypatch
):
    """The mandated end-to-end proof — real pack, real apply path."""
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = _load_caverns_and_claudes()
    assert pack.rules.ruleset == "wwn", "caverns_and_claudes must be bound ruleset: wwn"

    # ── Build a real Mage via the real builder/build() ─────────────────────
    caster_name = "Mei Lin"
    caster = _build_mage(pack, caster_name)
    assert caster.char_class == "Mage", (
        f"chargen must resolve the Mage class; got {caster.char_class!r}"
    )
    sc = caster.core.spellcasting
    assert sc is not None, "Mage must be seeded with a SpellcastingState"
    assert sc.prepared == _EXPECTED_PREPARED, (
        f"Mage starting_prepared must seed prepared; got {sc.prepared!r}"
    )
    assert _DAMAGE_SPELL in sc.prepared, "Mage must have wracking_bolt (damage spell) prepared"
    assert sc.casts_remaining == 2, f"Mage must start with 2 casts; got {sc.casts_remaining}"

    # ── Snapshot + seat the Dungeon Combat via the production seam ─────────
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(caster)

    opponent = "Jade Duelist"
    _seat_dungeon_combat(
        pack, snap, caster_name=caster_name, opponent=opponent, location="Courtyard"
    )

    opponent_core = snap.find_creature_core(opponent)
    assert opponent_core is not None, (
        "opponent core must be reachable via find_creature_core — without it the "
        "WWN cast has no defender HP to ablate"
    )
    assert opponent_core.armor_class == _OPPONENT_AC, "opponent AC from opponent_default_stats"
    assert opponent_core.hp.current == _OPPONENT_HP, "opponent HP from opponent_default_stats"

    # ── Pin rng: max roll → save made (20 >= target) → damage halved, but
    # still > 0 and below the kill threshold, so the proof is deterministic
    # and does not trip the downed seam. 1d6 max=6, halved=3 → opponent 10->7.
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: b)

    # ── Capture watcher events for the B/X negative assertion ──────────────
    hub = _RecordingHub()
    monkeypatch.setattr("sidequest.server.narration_apply._watcher_publish", hub)

    # ── Drive the cast_spell beat through the REAL apply path ──────────────
    # from_explicit_action=True bypasses the SOUL inferred-PC-beat gate the way
    # the dispatch path does (mirrors test_space_opera_hp_e2e Task 11). The
    # cast_spell BeatSelection carries the spell_id, exactly as a real cast.
    casts_before = sc.casts_remaining
    hp_before = opponent_core.hp.current

    result = NarrationTurnResult(
        narration="Mei Lin draws on the wracking page and looses a bolt of agony.",
        beat_selections=[
            BeatSelection(actor=caster_name, beat_id="cast_spell", spell_id=_DAMAGE_SPELL),
        ],
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

    # ── Assertion 1: the cast spent one cast (2 -> 1) ──────────────────────
    caster_after = snap.find_creature_core(caster_name)
    assert caster_after is not None
    sc_after = caster_after.spellcasting
    assert sc_after is not None
    assert sc_after.casts_remaining == casts_before - 1, (
        f"casting wracking_bolt must spend exactly one cast through the real apply "
        f"path; before={casts_before} after={sc_after.casts_remaining}"
    )

    # ── Assertion 2: opponent HP reduced (damage through the HP channel) ───
    assert opponent_core.hp.current < hp_before, (
        f"wracking_bolt damage must ablate the opponent's HP through the WWN cast "
        f"spine; before={hp_before} after={opponent_core.hp.current}"
    )

    # ── Assertion 3: wwn.spell.cast span fired (the lie detector) ──────────
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(spans) >= 1, (
        f"the WWN cast spine must emit a wwn.spell.cast span on the real apply path "
        f"(GM-panel lie detector); got span names "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert spans[-1].attributes.get("refused") is False, (
        "the cast was a valid cast (caster has the spell prepared + a cast remaining), "
        "so the wwn.spell.cast span must record refused=False"
    )

    # ── Assertion 4 (NEGATIVE): the B/X cast arm did NOT fire ──────────────
    # _resolve_innate_cast_for_beat publishes magic.cast_spell_* events. If the
    # wwn arm took the branch (as it must on a ruleset: wwn pack), NONE of them
    # fired. This is the path-trap proof: the wwn branch ran, not the B/X one.
    bx_events = [e for e in hub.events if e[0].startswith("magic.cast_spell")]
    assert not bx_events, (
        f"the B/X cast path (magic.cast_spell_*) must NOT fire on a ruleset: wwn "
        f"pack — the wwn arm must take the branch; got B/X events {bx_events!r}"
    )
