"""Pydantic round-trip tests for ``Npc.disposition`` as ``Disposition`` — Story 50-10.

After 50-10, ``Npc.disposition`` is no longer ``int`` — it is the
``Disposition`` wrapper. Two invariants matter for the cutover:

1. Backward compat — fixtures and existing integration tests construct
   NPCs with ``Npc(disposition=10)`` (raw int). That must keep working,
   coerced into ``Disposition(10)``, or hundreds of test sites need
   touching at once.
2. Persistence stability — game state round-trips through Pydantic
   (``model_dump`` / ``model_validate``) and through SQLite save files
   as JSON. The disposition's numeric value AND derived attitude must
   survive that round-trip losslessly.

These tests pin both invariants. They are deliberately decoupled from
the OTEL emission path (covered in test_disposition_call_site_migration.py)
so a failure here is unambiguous: the field model itself is wrong.
"""

from __future__ import annotations

import json

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.disposition import Attitude, Disposition
from sidequest.game.session import Npc


def _make_npc(name: str, disposition: int | Disposition = 0) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=disposition,
    )


# ---------------------------------------------------------------------------
# Construction: accepts both Disposition and raw int (AC3)
# ---------------------------------------------------------------------------


def test_npc_disposition_field_accepts_disposition_instance() -> None:
    """The canonical construction path — passing a Disposition object."""
    npc = _make_npc("Bartender", Disposition(15))
    assert isinstance(npc.disposition, Disposition), (
        f"expected Disposition, got {type(npc.disposition).__name__}"
    )
    assert npc.disposition.value == 15


def test_npc_disposition_field_accepts_raw_int_for_backward_compat() -> None:
    """Existing fixtures pass raw ints — they must coerce to Disposition
    silently. If this fails, every fixture in the integration suite breaks
    in a single commit. Story session SM Assessment flagged this risk."""
    npc = _make_npc("Bartender", 15)
    assert isinstance(npc.disposition, Disposition)
    assert npc.disposition.value == 15
    assert npc.disposition.attitude() == Attitude.FRIENDLY


def test_npc_default_disposition_is_zero_disposition() -> None:
    npc = _make_npc("Stranger")
    assert isinstance(npc.disposition, Disposition)
    assert npc.disposition.value == 0
    assert npc.disposition.attitude() == Attitude.NEUTRAL


def test_two_npcs_with_default_disposition_have_independent_state() -> None:
    """Python rule #2: a mutable default at class level shares state
    across instances. ``Npc.disposition`` must use ``default_factory``
    (or equivalent) so two NPCs default-constructed do not share a
    Disposition object."""
    a = _make_npc("A")
    b = _make_npc("B")
    assert a.disposition is not b.disposition, (
        "two default-constructed NPCs share the same Disposition object — "
        "use Field(default_factory=Disposition) at the model level"
    )


def test_npc_disposition_clamps_when_set_from_raw_int_above_bound() -> None:
    npc = _make_npc("Zealot", 200)
    assert npc.disposition.value == 100
    assert npc.disposition.attitude() == Attitude.FRIENDLY


def test_npc_disposition_clamps_when_set_from_raw_int_below_bound() -> None:
    npc = _make_npc("Foe", -200)
    assert npc.disposition.value == -100
    assert npc.disposition.attitude() == Attitude.HOSTILE


# ---------------------------------------------------------------------------
# Round-trip: model_dump → model_validate (AC3)
# ---------------------------------------------------------------------------


def test_npc_disposition_round_trips_via_model_dump_and_validate() -> None:
    """Game state serializes via Pydantic for save files (ADR-023). A
    Disposition that round-trips lossily would corrupt every save."""
    original = _make_npc("Guard", Disposition(25))
    dumped = original.model_dump()
    restored = Npc.model_validate(dumped)

    assert isinstance(restored.disposition, Disposition)
    assert restored.disposition.value == 25
    assert restored.disposition.attitude() == Attitude.FRIENDLY


def test_npc_disposition_round_trips_via_json_dump_and_validate() -> None:
    """SQLite save files store JSON blobs — verify the JSON round-trip
    also preserves both the numeric value and the derived attitude."""
    original = _make_npc("Thief", Disposition(-15))
    blob = original.model_dump_json()
    restored = Npc.model_validate_json(blob)

    assert isinstance(restored.disposition, Disposition)
    assert restored.disposition.value == -15
    assert restored.disposition.attitude() == Attitude.HOSTILE


def test_npc_disposition_json_payload_is_bare_integer() -> None:
    """The JSON form of ``Npc.disposition`` must be a bare int — that's
    the contract the GM panel and save-file reader expect. A wrapper
    shape (``{"value": 15}``, ``"Disposition(15)"``, etc.) would corrupt
    every save and break the panel's numeric read path.

    Locks the exact wire shape via type+value equality, not substring
    matching — a substring check on ``json.dumps()`` would pass on a
    coincidental ``15`` somewhere else in the encoded sub-object."""
    npc = _make_npc("Bystander", Disposition(15))
    payload = json.loads(npc.model_dump_json())
    assert payload["disposition"] == 15, (
        f"disposition must serialize to bare int 15, got {payload['disposition']!r} "
        f"(type {type(payload['disposition']).__name__})"
    )
    assert isinstance(payload["disposition"], int), (
        f"disposition JSON shape must be int, got {type(payload['disposition']).__name__}: "
        f"{payload['disposition']!r}"
    )


# ---------------------------------------------------------------------------
# Boundary cases at the field layer (defensive duplication of unit tests)
# ---------------------------------------------------------------------------


def test_npc_disposition_strict_neutral_boundary_at_ten() -> None:
    npc = _make_npc("Vendor", 10)
    assert npc.disposition.attitude() == Attitude.NEUTRAL


def test_npc_disposition_friendly_just_above_boundary() -> None:
    npc = _make_npc("Patron", 11)
    assert npc.disposition.attitude() == Attitude.FRIENDLY


def test_npc_disposition_hostile_just_below_negative_boundary() -> None:
    npc = _make_npc("Rival", -11)
    assert npc.disposition.attitude() == Attitude.HOSTILE
