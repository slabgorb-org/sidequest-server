"""RED (story 158-55, defect 2) — the WWN test-fixture pack must resolve a
tier-1 encounter so chargen-wiring tests seed for real instead of leaning on
the ``_isolate_monster_manuals`` tolerance swallow (a live No-Silent-Fallbacks
violation that masks a fixture gap).

Ground truth captured during test design (2026-07-08):
  - ``wwn_test_pack/test_world``      -> seeds OK (2 encounters, tier1=1)
  - ``wwn_test_pack/flickering_reach`` -> EncounterSeedError (world absent -> no
    bestiary -> 90-1 fail-loud). This is the exact error the ~7 chargen-wiring
    tests hit today; ``tests/conftest.py::_isolate_monster_manuals`` swallows it
    so those tests pass on a masked gap.

The fix (story-literal): add a hermetic ``flickering_reach`` WWN world with a
tier-1 bestiary to the fixture pack, repoint the chargen-wiring tests off the
phantom ``caverns_and_claudes/flickering_reach`` combo, and drop the swallow.

Modeled on tests/server/dispatch/test_pregen_bestiary_90_1.py — real pack ->
real ``seed_manual`` (module-direct import, so the ``_isolate_monster_manuals``
attribute patch does NOT intercept it) -> non-empty tier-1 pool, asserted on
behavior.
"""

from __future__ import annotations

import random
from pathlib import Path

from sidequest.game.monster_manual import MonsterManual
from sidequest.server.dispatch.pregen import seed_manual

FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "packs"

# The hermetic WWN world the chargen-wiring tests must bind. It does not exist
# yet — that IS the gap. Dev adds worlds/flickering_reach/ (bestiary.yaml with a
# tier-1 stat block) to wwn_test_pack.
_GENRE = "wwn_test_pack"
_WORLD = "flickering_reach"


def test_wwn_fixture_flickering_reach_resolves_a_tier1_encounter() -> None:
    """Seeding the WWN fixture's ``flickering_reach`` world yields >=1 tier-1
    encounter through the REAL seed path — no EncounterSeedError, no tolerance
    swallow. RED today: the world (and its bestiary) does not exist."""
    manual = MonsterManual(genre=_GENRE, world=_WORLD)
    seed_manual(
        genre_packs_path=FIXTURE_PACKS_DIR,
        genre=_GENRE,
        world=_WORLD,
        manual=manual,
        rng=random.Random(15855),
    )
    tier1 = [e for e in manual.encounters if e.tier == 1]
    assert tier1, (
        f"{_GENRE}/{_WORLD} resolved no tier-1 WWN encounter — the fixture world "
        "must ship a worlds/flickering_reach/bestiary.yaml with a tier-1 stat "
        "block so chargen-wiring tests seed for real instead of relying on the "
        "_isolate_monster_manuals tolerance swallow (story 158-55 defect 2)"
    )
