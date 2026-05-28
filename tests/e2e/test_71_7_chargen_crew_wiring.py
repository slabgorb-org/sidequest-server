"""Story 71-7 (RED) — wiring against shipping coyote_star content.

Reproduces the chargen first-commit seam exactly as
``CharGenMixin._chargen_confirmation`` sequences it
(``chargen_mixin.py``: ``materialize_from_genre_pack(...)`` then
``preload_authored_npcs(...)``) and asserts the OUTCOME the player needs: the
authored Kestrel crew + Dura Mendes land in ``snapshot.npcs`` with their
seeded dispositions, and ``npc.authored_loaded`` fires per crew member.

This is the load-bearing wiring test: the unit tests at
``tests/game/test_71_7_authored_crew_fresh_gate.py`` pin the gate contract
against synthetic fixtures; this proves it against the real shipping content
that broke in the live session (session_id=76).

It also pins the corrected precondition — a real fresh materialization carries
``interaction == 1`` and ``characters == []`` — so the bug can never be masked
again by a fixture that fabricates ``interaction == 0``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sidequest.game.world_materialization import (
    CampaignMaturity,
    materialize_from_genre_pack,
    preload_authored_npcs,
)
from sidequest.genre.loader import GenreLoader
from sidequest.server.session_helpers import _world_history_value
from sidequest.telemetry.spans import SPAN_NPC_AUTHORED_LOADED, Span

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

EXPECTED_DISPOSITIONS = {
    "Wainu Moana-Teru": 60,
    "Hubo Dicia": 55,
    "Kuna-Mikkaan": 50,
    "Kanga Moana-Teru": 60,
    "Dura Mendes": 0,
}


def _materialize_coyote_star_fresh():
    loader = GenreLoader([CONTENT_ROOT])
    pack = loader.load("space_opera")
    world = pack.worlds["coyote_star"]
    history_value = _world_history_value(pack, "coyote_star")
    materialized = materialize_from_genre_pack(
        history_value, CampaignMaturity.Fresh, "space_opera", "coyote_star"
    )
    return world, materialized


def test_coyote_star_fresh_precondition_interaction_is_one() -> None:
    """Document the real trigger: fresh coyote_star materialization carries
    interaction == 1 and no player character — the exact state the production
    chargen seam hands to preload."""
    _world, materialized = _materialize_coyote_star_fresh()
    assert materialized.turn_manager.interaction == 1
    assert materialized.characters == []


def test_chargen_seam_loads_authored_crew_into_npcs() -> None:
    """AC1: replaying the production seam against real content hydrates the
    authored crew with correct dispositions."""
    world, materialized = _materialize_coyote_star_fresh()

    # Production order: materialize, then preload (chargen_mixin.py:740,786).
    preload_authored_npcs(materialized, world.authored_npcs)

    loaded = {n.core.name: n.disposition for n in materialized.npcs}
    for name, disp in EXPECTED_DISPOSITIONS.items():
        assert name in loaded, (
            f"authored crew member {name!r} did not hydrate via the chargen seam; "
            f"npcs = {sorted(loaded)!r}"
        )
        assert loaded[name] == disp, f"{name} disposition {loaded[name]} != authored {disp}"


def test_chargen_seam_emits_authored_loaded_span_per_crew() -> None:
    """AC4: the real seam emits one ``npc.authored_loaded`` span per authored
    NPC (GM-panel observability — CLAUDE.md)."""
    world, materialized = _materialize_coyote_star_fresh()

    with patch.object(Span, "open", wraps=Span.open) as span_open:
        preload_authored_npcs(materialized, world.authored_npcs)

    loaded_spans = [
        c for c in span_open.call_args_list if c.args and c.args[0] == SPAN_NPC_AUTHORED_LOADED
    ]
    assert len(loaded_spans) == len(world.authored_npcs), (
        f"expected {len(world.authored_npcs)} '{SPAN_NPC_AUTHORED_LOADED}' spans, "
        f"got {len(loaded_spans)}"
    )
