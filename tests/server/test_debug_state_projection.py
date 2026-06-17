"""RED suite for story 124-4 — honest data behind the Inspector State tab.

The State-tab redesign (epic 124) centerpieces three graphics — the OCEAN
sparkline, the trope progression bar plot, and the inventory narrative-weight
bars. All three must bind to REAL data (no-fabrication rule). Investigation of
the live ``debug_state`` projection (``sidequest/server/rest.py`` ~:466-579)
found that two of them are fed dead reads, and the third is never populated:

  1. INVENTORY — every player is emitted with ``inventory={"items": [], ...}``
     (rest.py:554-557). The character's real items
     (``char.core.inventory.items``) are never read, so the weight-bar table
     has nothing to render.

  2. TROPE ID — the projection reads ``getattr(trope, "trope_id", "")`` but
     ``TropeState`` has no ``trope_id`` field (it is ``id``). With
     ``model_config = {"extra": "ignore"}`` the read returns the default ``""``,
     so every trope row shows a BLANK id in production.

  3. TROPE PROGRESSION — the projection reads
     ``int(getattr(trope, "progression", 0) or 0)`` but the field is
     ``progress: float`` (not ``progression``). The read returns ``0`` for every
     trope, and the ``int()`` cast would truncate a real 0..1 value anyway. So
     the progression bar is pinned at 0 for every trope — a textbook
     "convincing prose, zero mechanical backing" lie the GM panel exists to
     catch.

To test this with synthetic snapshots (the fixture-driven pattern this repo
prefers over DB-coupled endpoint tests — see server CLAUDE.md "No Source-Text
Wiring Tests"), the inline projection must be EXTRACTED from the async route
into a pure function ``project_session_state_view(snap, *, session_key,
last_activity_ts) -> dict``. These tests import that function; the import is the
first thing to turn green (extraction), then the three assertions drive the
field fixes. Dev may relocate the helper via a logged deviation, but the
behavioral contract below is fixed.
"""

from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, Npc, TropeState

# Extraction target (does not exist yet — RED). Dev lifts the inline projection
# block out of rest.py::debug_state into this pure, snapshot-only function.
from sidequest.server.state_projection import project_session_state_view


def _project(snap: GameSnapshot) -> dict:
    return project_session_state_view(
        snap, session_key="fantasy/aetheria-reach", last_activity_ts=1000
    )


def _character_with_items(items: list[dict]) -> Character:
    core = CreatureCore(
        name="Kaelen Vire",
        description="A warden of the drowned halls.",
        personality="dutiful",
        inventory=Inventory(items=items, gold=340),
    )
    return Character(core=core, char_class="Warden", race="Human", backstory="A warden.")


# ---------------------------------------------------------------------------
# Inventory population
# ---------------------------------------------------------------------------


def test_projection_surfaces_character_inventory_items() -> None:
    """players[].inventory.items must come from char.core.inventory.items."""
    snap = GameSnapshot(
        genre_slug="fantasy",
        world_slug="aetheria-reach",
        characters=[
            _character_with_items(
                [
                    {"name": "Emberbrand Glaive", "narrative_weight": 0.86, "state": "attuned"},
                    {"name": "Frayed rope", "narrative_weight": 0.08, "state": "stored"},
                ]
            )
        ],
    )

    view = _project(snap)

    assert len(view["players"]) == 1
    items = view["players"][0]["inventory"]["items"]
    assert len(items) == 2, "inventory items must be projected, not hard-coded empty"
    names = {it["name"] for it in items}
    assert names == {"Emberbrand Glaive", "Frayed rope"}


def test_projection_preserves_item_narrative_weight() -> None:
    """The weight-bar table needs the real narrative_weight per item."""
    snap = GameSnapshot(
        genre_slug="fantasy",
        characters=[
            _character_with_items(
                [{"name": "Emberbrand Glaive", "narrative_weight": 0.86, "state": "attuned"}]
            )
        ],
    )

    view = _project(snap)

    glaive = view["players"][0]["inventory"]["items"][0]
    assert glaive["narrative_weight"] == pytest.approx(0.86)


def test_projection_emits_empty_inventory_without_fabrication() -> None:
    """A character with no items projects an empty list — never invented rows."""
    snap = GameSnapshot(genre_slug="fantasy", characters=[_character_with_items([])])

    view = _project(snap)

    assert view["players"][0]["inventory"]["items"] == []


# ---------------------------------------------------------------------------
# Trope id + progression fidelity
# ---------------------------------------------------------------------------


def test_projection_reads_real_trope_id() -> None:
    """trope_definition_id must come from TropeState.id, not the absent trope_id."""
    snap = GameSnapshot(
        genre_slug="fantasy",
        active_tropes=[TropeState(id="reluctant_hero", status="active", progress=0.88)],
    )

    view = _project(snap)

    assert view["trope_states"][0]["trope_definition_id"] == "reluctant_hero"


def test_projection_preserves_fractional_trope_progression() -> None:
    """progression must read TropeState.progress (float), not be zeroed/int-truncated."""
    snap = GameSnapshot(
        genre_slug="fantasy",
        active_tropes=[
            TropeState(id="mentor_betrayal", status="active", progress=0.62),
            TropeState(id="broken_oath", status="resolved", progress=1.0),
            TropeState(id="lost_heir", status="dormant", progress=0.3),
        ],
    )

    view = _project(snap)

    by_id = {t["trope_definition_id"]: t for t in view["trope_states"]}
    assert by_id["mentor_betrayal"]["progression"] == pytest.approx(0.62)
    assert by_id["lost_heir"]["progression"] == pytest.approx(0.3)
    assert by_id["broken_oath"]["progression"] == pytest.approx(1.0)
    # The int()-cast bug would collapse 0.62 -> 0; guard against any regression.
    assert by_id["mentor_betrayal"]["progression"] != 0


def test_projection_carries_trope_status() -> None:
    snap = GameSnapshot(
        genre_slug="fantasy",
        active_tropes=[TropeState(id="reluctant_hero", status="active", progress=0.5)],
    )

    view = _project(snap)

    assert view["trope_states"][0]["status"] == "active"


# ---------------------------------------------------------------------------
# NPC hp — read the real HpPool, not the absent `edge`
# ---------------------------------------------------------------------------


def test_projection_reads_npc_hp_from_hp_pool() -> None:
    """hp/max_hp must come from core.hp (HpPool), not the absent `core.edge`.

    rest.py:481-485 reads ``getattr(core, "edge", None).maximum`` — but
    CreatureCore was re-pointed edge→hp and HpPool's field is ``max`` (not
    ``maximum``), so every NPC currently projects 0/0. The registry HP column
    must show the real pool.
    """
    npc = Npc(
        core=CreatureCore(
            name="Brakka Stonejaw",
            description="A smith.",
            personality="gruff",
            hp=HpPool(current=34, max=40, base_max=40),
        ),
    )
    snap = GameSnapshot(genre_slug="fantasy", npcs=[npc])

    view = _project(snap)

    entry = view["npc_registry"][0]
    assert entry["hp"] == 34
    assert entry["max_hp"] == 40


# ---------------------------------------------------------------------------
# OCEAN passthrough (characterization — the UI sparkline reads `ocean`)
# ---------------------------------------------------------------------------


def test_projection_passes_through_npc_ocean_dict() -> None:
    """The live `ocean` dict is the source of truth for the OCEAN glyph.

    `ocean_summary` is intentionally None on the wire (the UI reads `ocean`),
    so the projection must carry the five-dimension dict through intact.
    """
    ocean = {
        "openness": 8.0,
        "conscientiousness": 7.0,
        "extraversion": 3.5,
        "agreeableness": 6.2,
        "neuroticism": 4.8,
    }
    npc = Npc(
        core=CreatureCore(name="Sister Veil", description="Veiled oracle.", personality="serene"),
        ocean=ocean,
    )
    snap = GameSnapshot(genre_slug="fantasy", npcs=[npc])

    view = _project(snap)

    entry = view["npc_registry"][0]
    assert entry["name"] == "Sister Veil"
    assert entry["ocean"] == ocean
    assert set(entry["ocean"].keys()) == {
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    }
    # Documents the dead field: the UI must not depend on ocean_summary.
    assert entry["ocean_summary"] is None
