"""Story 102-2 AC3 (data seam) — the CONFRONTATION payload projects the
recipient's WWN spellcasting state so the overlay can render the spell picker.

The overlay's "Work a Spell" picker needs the prepared-spell list and
``casts_remaining`` (player-visible math — Sebastien/Jade). The server
already DERIVES the recipient's ``SpellcastingState`` inside
``build_confrontation_payload`` (the Task 5 cast_spell gate) but never emits
it — the client currently has no projection of the WWN cast economy at all.

Contract pinned here:

  - For a WWN caster recipient, the payload carries a ``spellcasting`` block:
    ``casts_remaining``, ``casts_per_day``, and ``prepared`` (the spell-id
    list the picker offers).
  - For a recipient with no spellcasting state (Warrior, B/X packs), the
    block is None/absent — never a fabricated empty economy.

Uses the REAL heavy_metal pack (cast_spell class_filter includes Necromancer);
skips when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_SPELL = "wracking_bolt"

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_encounter(player: str, opponent: str):
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=player, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _combat_cdef(pack):
    cdef = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert cdef is not None, "heavy_metal must expose a 'combat' confrontation"
    assert any(b.id == "cast_spell" for b in cdef.beats), (
        "heavy_metal combat must author a cast_spell beat"
    )
    return cdef


def _class_def(pack, display_name: str):
    cls = next((c for c in pack.classes if c.display_name == display_name), None)
    assert cls is not None, f"heavy_metal classes.yaml must ship {display_name!r}"
    return cls


def _build(pack, *, class_display: str, spellcasting):
    from sidequest.server.dispatch.confrontation import build_confrontation_payload

    return build_confrontation_payload(
        encounter=_make_encounter("Vesska", "Furnace Thrall"),
        cdef=_combat_cdef(pack),
        genre_slug="heavy_metal",
        recipient_pc=(_class_def(pack, class_display), 0.0, None),
        recipient_actor_name="Vesska",
        spellcasting=spellcasting,
    )


def test_payload_projects_wwn_spellcasting_for_a_caster():
    """A Necromancer recipient with prepared spells must see the cast economy
    on the wire — the picker cannot render what the server hides."""
    from sidequest.game.wwn_magic import SpellcastingState

    pack = _load_heavy_metal()
    payload = _build(
        pack,
        class_display="Necromancer",
        spellcasting=SpellcastingState(
            prepared=[_SPELL],
            casts_remaining=2,
            casts_per_day=2,
            max_spell_level=1,
        ),
    )

    block = payload.get("spellcasting")
    assert block is not None, (
        "a WWN caster's CONFRONTATION payload must carry a 'spellcasting' "
        "block — the overlay picker has no other source for prepared spells "
        f"and casts_remaining; payload keys: {sorted(payload.keys())}"
    )
    assert block["casts_remaining"] == 2
    assert block["casts_per_day"] == 2
    assert block["prepared"] == [_SPELL]

    # Sanity: the beat the picker attaches to survived the recipient filter.
    beat_ids = [b["id"] for b in payload["beats"]]
    assert "cast_spell" in beat_ids, (
        f"the Necromancer must still be offered cast_spell; got {beat_ids}"
    )


def test_payload_omits_spellcasting_for_a_non_caster():
    """A Warrior (no SpellcastingState) must get None/absent — fabricating an
    empty economy would make the overlay render a dead picker."""
    pack = _load_heavy_metal()
    payload = _build(pack, class_display="Warrior", spellcasting=None)

    assert payload.get("spellcasting") is None, (
        "a non-caster recipient must not receive a spellcasting block; got "
        f"{payload.get('spellcasting')!r}"
    )
