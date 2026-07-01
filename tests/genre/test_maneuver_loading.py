"""RED tests — Story 158-39 — dogfight maneuver metadata loading (ADR-153 §4, Task 1).

The opponent brain gates its pick by energy affordability and expresses attitude
through maneuver CLASS. The legal maneuver ids already load (via
``interaction_table.maneuvers_consumed``), but each maneuver's ``class`` and
``energy_cost`` (authored in ``dogfight/maneuvers_mvp.yaml``) are NOT loaded onto
the ConfrontationDef today. This module pins:

1. A ``ManeuverDef`` model exposing ``id`` / ``maneuver_class`` (YAML ``class:``
   alias) / ``energy_cost`` (default 0, negative = recovery).
2. The loader resolving a ``maneuvers: {_from: ...}`` pointer onto
   ``ConfrontationDef.maneuvers`` for the FIXTURE pack (``swn_test_pack``) — so a
   content-only change can never turn this server test red (project rule).

RED shape: ``ManeuverDef`` does not exist yet (ImportError at collection), and the
fixture pack's dogfight def has no ``maneuvers:`` pointer. Dev (158-39 GREEN)
adds the model + ``maneuvers`` field (``sidequest/genre/models/rules.py``), the
``_from:`` resolution (``sidequest/genre/loader.py``), and the pointer in the
fixture pack's ``rules.yaml`` dogfight def (mirroring the live space_opera pack).
"""

from __future__ import annotations

from sidequest.genre.models.rules import ManeuverDef
from sidequest.server.dispatch.confrontation import find_confrontation_def
from tests.fixtures.dogfight_playtest_encounter import make_dogfight_pack

_DOGFIGHT_TYPE = "dogfight"
# The MVP menu authored in dogfight/maneuvers_mvp.yaml (class + energy_cost).
_EXPECTED = {
    "straight": ("passive", -5),
    "bank": ("evasive", 5),
    "loop": ("offensive", 30),
    "kill_rotation": ("offensive_space_only", 5),
}


# ---------------------------------------------------------------------------
# ManeuverDef model
# ---------------------------------------------------------------------------


def test_maneuver_def_fields() -> None:
    m = ManeuverDef(id="loop", **{"class": "offensive"}, energy_cost=30)
    assert m.id == "loop"
    assert m.maneuver_class == "offensive"
    assert m.energy_cost == 30


def test_maneuver_def_class_is_aliased_from_yaml_class_key() -> None:
    """maneuvers_mvp.yaml uses the reserved word ``class:`` — the model exposes it
    as ``maneuver_class`` via a pydantic alias so the field name is valid Python."""
    m = ManeuverDef(**{"id": "bank", "class": "evasive", "energy_cost": 5})
    assert m.maneuver_class == "evasive"


def test_maneuver_def_energy_cost_negative_is_recovery() -> None:
    m = ManeuverDef(id="straight", **{"class": "passive"}, energy_cost=-5)
    assert m.energy_cost == -5


def test_maneuver_def_energy_cost_defaults_zero() -> None:
    """A maneuver authored without an explicit cost is free, not an error."""
    m = ManeuverDef(id="hold", **{"class": "passive"})
    assert m.energy_cost == 0


# ---------------------------------------------------------------------------
# Loader wiring — maneuvers resolve onto the ConfrontationDef (fixture pack)
# ---------------------------------------------------------------------------


def test_loader_resolves_maneuvers_onto_dogfight_def() -> None:
    """The wiring proof (Task 1): loading the fixture ``swn_test_pack`` through the
    production loader populates ``ConfrontationDef.maneuvers`` with the class +
    energy_cost the brain needs — proving the ``maneuvers: {_from:}`` pointer is
    resolved by the real loader, not just modelled."""
    pack = make_dogfight_pack()
    cdef = find_confrontation_def(
        pack.rules.confrontations if pack.rules else [], _DOGFIGHT_TYPE
    )
    assert cdef is not None, "fixture pack has no dogfight ConfrontationDef"

    by_id = {m.id: m for m in cdef.maneuvers}
    assert set(by_id) == set(_EXPECTED), (
        f"loaded maneuvers {sorted(by_id)} != authored menu {sorted(_EXPECTED)} — "
        "the maneuvers _from: pointer was not resolved onto the dogfight def"
    )
    for mid, (cls, cost) in _EXPECTED.items():
        assert by_id[mid].maneuver_class == cls, f"{mid} class"
        assert by_id[mid].energy_cost == cost, f"{mid} energy_cost"


def test_loaded_maneuvers_cover_the_legal_menu() -> None:
    """Every legal maneuver (``interaction_table.maneuvers_consumed``) has loaded
    metadata — otherwise the brain would gate a legal maneuver it can't price."""
    pack = make_dogfight_pack()
    cdef = find_confrontation_def(
        pack.rules.confrontations if pack.rules else [], _DOGFIGHT_TYPE
    )
    assert cdef is not None and cdef.interaction_table is not None
    legal = set(cdef.interaction_table.maneuvers_consumed)
    have = {m.id for m in cdef.maneuvers}
    assert legal <= have, f"legal maneuvers {sorted(legal - have)} have no loaded metadata"
