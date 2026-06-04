"""Story 77-3 RED — promote quest_anchors to a first-class WorldStatePatch field.

These tests capture the reconciled story (see session 77-3 Design Deviations):
the codebase has NO ``world_data_updates`` dict, and ``GameSnapshot.quest_anchors``
is already first-class. The real gap is that ``WorldStatePatch`` cannot carry
quest_anchors, so a narrator world-patch can't flow anchors into the snapshot
(today only the ``record_quest`` tool mutates the snapshot directly).

ACs covered here:
- AC1: WorldStatePatch.quest_anchors field + pydantic round-trip
        (model_dump/model_validate — NOT to_dict/from_dict, which don't exist).
- AC2: apply_world_patch merges patch.quest_anchors -> snapshot.quest_anchors
        via DEDUP-APPEND (order-preserving union), guarded ``is not None`` —
        per Architect (Neo) reconcile ruling in session 77-3. A world-patch must
        NOT clobber the seeded campaign spine (the ADR-137 failure mode), so the
        merge is a union with the existing anchors, matching the only other two
        writers (quest_seed.py, record_quest.py). Consequence: there is no
        "clear" semantic — an empty patch list is a no-op union; None is "no
        change". compute_courses signature is UNCHANGED (reads the snapshot).
- AC4: malformed quest_anchors raises loudly (pydantic), never a silent [].
- AC5: apply_world_patch emits the ``world.patch.quest_anchors_present`` span
        attribute (boolean) on every patch that touches quest_anchors.

RED honesty note: WorldStatePatch sets ``model_config = {"extra": "forbid"}``,
so passing ``quest_anchors=...`` today raises ``ValidationError`` with an
``extra_forbidden`` error. The negative (AC4) tests therefore assert a
*field-type* error (string_type / list_type) at loc ``quest_anchors`` and the
ABSENCE of an ``extra_forbidden`` error — so they fail for the right reason in
RED (only extra_forbidden fires) and pass in GREEN (field exists, type rejected).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.session import GameSnapshot, WorldStatePatch


def _patch_validation_errors(**kwargs: object) -> list[dict]:
    """Construct a WorldStatePatch expecting failure; return pydantic errors()."""
    with pytest.raises(ValidationError) as exc_info:
        WorldStatePatch(**kwargs)  # type: ignore[arg-type]
    return list(exc_info.value.errors())


def _quest_anchors_field_type_error(errors: list[dict]) -> bool:
    """True iff some error is a TYPE error on the quest_anchors field.

    Distinguishes the GREEN-state type rejection (string_type / list_type at
    loc[0]=='quest_anchors') from the RED-state ``extra_forbidden`` error.
    """
    return any(
        bool(e.get("loc")) and e["loc"][0] == "quest_anchors" and e["type"] != "extra_forbidden"
        for e in errors
    )


# ---------------------------------------------------------------------------
# AC1 — field exists, defaults, round-trip
# ---------------------------------------------------------------------------


def test_world_patch_accepts_quest_anchors_field() -> None:
    """AC1: WorldStatePatch carries a list[str] quest_anchors field."""
    patch = WorldStatePatch(quest_anchors=["deep_root", "the_gate"])
    assert patch.quest_anchors == ["deep_root", "the_gate"]


def test_world_patch_quest_anchors_defaults_to_none() -> None:
    """AC1: unset means 'no change' — the field defaults to None, not []."""
    patch = WorldStatePatch()
    assert patch.quest_anchors is None


def test_world_patch_quest_anchors_round_trips_via_model_validate() -> None:
    """AC1: the field survives model_dump -> model_validate unchanged.

    (Deviation from AC wording: WorldStatePatch is pydantic; it has no
    to_dict/from_dict — round-trip is model_dump/model_validate.)
    """
    patch = WorldStatePatch(quest_anchors=["mid", "far"])
    payload = patch.model_dump()
    restored = WorldStatePatch.model_validate(payload)
    assert restored.quest_anchors == ["mid", "far"]


def test_world_patch_round_trips_empty_quest_anchors() -> None:
    """AC1/AC2: an explicit empty list is distinct from None and round-trips."""
    patch = WorldStatePatch(quest_anchors=[])
    restored = WorldStatePatch.model_validate(patch.model_dump())
    assert restored.quest_anchors == []


def test_world_patch_lacking_quest_anchors_key_loads_as_default() -> None:
    """Load-safety (residual of dropped AC3, per Neo ruling): a payload with no
    quest_anchors key — i.e. any save/patch written before this story — loads as
    the None default, never raising and never inventing anchors."""
    restored = WorldStatePatch.model_validate({"atmosphere": "fog"})
    assert restored.quest_anchors is None


# ---------------------------------------------------------------------------
# AC2 — apply_world_patch merges into the snapshot
# ---------------------------------------------------------------------------


def test_apply_world_patch_adds_quest_anchors_to_empty_snapshot() -> None:
    """AC2: a patch with quest_anchors flows onto an empty snapshot.quest_anchors."""
    snap = GameSnapshot()
    snap.apply_world_patch(WorldStatePatch(quest_anchors=["mid", "far"]))
    assert snap.quest_anchors == ["mid", "far"]


def test_apply_world_patch_none_quest_anchors_preserves_existing() -> None:
    """AC2: None means 'no change' — existing anchors are not wiped."""
    snap = GameSnapshot(quest_anchors=["deep_root"])
    snap.apply_world_patch(WorldStatePatch(atmosphere="tense"))
    assert snap.quest_anchors == ["deep_root"]


def test_apply_world_patch_dedup_appends_anchors_preserving_spine() -> None:
    """AC2 (Neo ruling): merge is an order-preserving UNION, not a replace.

    A narrator world-patch must never clobber the seeded campaign spine. New
    anchors append after existing ones; a duplicate is not re-added.
    """
    snap = GameSnapshot(quest_anchors=["deep_root"])
    snap.apply_world_patch(WorldStatePatch(quest_anchors=["far", "deep_root"]))
    assert snap.quest_anchors == ["deep_root", "far"]


def test_apply_world_patch_empty_list_is_noop_union() -> None:
    """AC2 (Neo ruling): there is no 'clear' semantic. An empty patch list
    unions to nothing-new, leaving the seeded spine intact — never wiped."""
    snap = GameSnapshot(quest_anchors=["deep_root"])
    snap.apply_world_patch(WorldStatePatch(quest_anchors=[]))
    assert snap.quest_anchors == ["deep_root"]


# ---------------------------------------------------------------------------
# AC4 — no silent fallbacks: malformed data raises loudly
# ---------------------------------------------------------------------------


def test_world_patch_rejects_non_string_quest_anchor_items() -> None:
    """AC4: a list of non-strings is malformed and must raise (no coercion)."""
    errors = _patch_validation_errors(quest_anchors=[1, 2, 3])
    assert _quest_anchors_field_type_error(errors), (
        "expected a type error on quest_anchors items; "
        f"got {[(e.get('loc'), e.get('type')) for e in errors]}"
    )


def test_world_patch_rejects_non_list_quest_anchors() -> None:
    """AC4: a bare string (not a list) is malformed and must raise."""
    errors = _patch_validation_errors(quest_anchors="deep_root")
    assert _quest_anchors_field_type_error(errors), (
        "expected a list_type error on quest_anchors; "
        f"got {[(e.get('loc'), e.get('type')) for e in errors]}"
    )


# ---------------------------------------------------------------------------
# AC5 — OTEL: world.patch.quest_anchors_present fires on touch
# ---------------------------------------------------------------------------


def test_apply_world_patch_emits_quest_anchors_present_attr(otel_capture) -> None:
    """AC5: applying a patch that touches quest_anchors sets the boolean
    ``world.patch.quest_anchors_present`` attribute on the apply_world_patch span."""
    snap = GameSnapshot()
    snap.apply_world_patch(WorldStatePatch(quest_anchors=["mid"]))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "apply_world_patch"]
    assert spans, "apply_world_patch span did not fire"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("world.patch.quest_anchors_present") is True


def test_apply_world_patch_quest_anchors_present_false_when_absent(otel_capture) -> None:
    """AC5: a patch that does NOT touch quest_anchors reports present=False
    (explicit signal, never a missing attribute the GM panel must guess at)."""
    snap = GameSnapshot()
    snap.apply_world_patch(WorldStatePatch(atmosphere="calm"))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "apply_world_patch"]
    assert spans, "apply_world_patch span did not fire"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("world.patch.quest_anchors_present") is False
