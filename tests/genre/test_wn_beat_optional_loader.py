"""Story 108-7 — WN combat defs go beat-optional (RED phase).

Two loader relaxations, BOTH gated on the bound ruleset owning combat (the
``WithoutNumberRulesetModule`` family — swn/wwn/cwn/awn) so native packs keep
failing loud (ADR-143 "Bind the Ruleset, Don't Balance It"; ADR-142):

1. **Beat-count gate moves to the loader and goes WN-aware.** Today
   ``ConfrontationDef._validate`` (a pydantic model validator, rules.py:~588)
   raises *unconditionally* when a def has zero beats. The model only sees the
   def, never the pack ruleset, so this check must MOVE to the loader (which
   knows ``rules.ruleset``). After the move, a ``category=combat`` /
   ``win_condition=hp_depletion`` def under a WN binding may author ZERO beats
   (the WN initiative engine, wn_round.py, owns attack/move/item-use/cast —
   story 108-1). A *native* pack with zero beats, and a non-combat (dial)
   confrontation even under WN, must STILL raise.

2. **encounter_beat_choices↔combat-beats coupling goes WN-aware.**
   ``_validate_class_filter_refs`` (loader.py:~696) raises when an
   ``allowed_classes`` class declares empty ``encounter_beat_choices``. Under a
   WN binding the WN engine owns the action set, so WN classes may leave it
   empty/absent. Native packs must STILL raise.

3. **OTEL (AC5).** The loader already emits ``state_transition`` watcher spans
   on load (``world_items``/``world_lore``/``genre_pack`` …) so the GM panel can
   prove a read fired. The beat-optional relaxation is the kind of silent gate
   weakening the lie-detector exists to catch, so allowing a beatless WN combat
   def MUST emit a ``state_transition`` span (``field == "wn_beat_optional"``)
   carrying the ruleset + confrontation_type.

These tests drive at the production ``load_genre_pack`` boundary (relaxation 1's
gate is unreachable from an isolated ``ConfrontationDef`` — the model can't see
the ruleset) and at ``_validate_class_filter_refs`` directly (its native mirror
has no live fixture — no native fixture pack ships a classes.yaml). Fixtures
only: never the live WWN packs (AC4 "all three WWN packs load once 108-3 strips
them" is verified by 108-3, not here — today the live packs still carry beats).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import sidequest.telemetry.watcher_hub as hub_mod
from sidequest.genre.error import GenreError, PackError
from sidequest.genre.loader import _validate_class_filter_refs, load_genre_pack
from tests._helpers.fixture_packs import WWN_TEST_PACK, fixture_pack_path, load_fixture_pack

NATIVE_TEST_PACK = "test_genre"  # ruleset defaults to "native"; ships a combat def, no classes.yaml


# ---------------------------------------------------------------------------
# Fixture-pack mutation helpers (clone to tmp, edit YAML, reload through the
# production loader — no shortcuts, mirrors tests/genre/test_loader.py).
# ---------------------------------------------------------------------------


def _clone_pack(slug: str, parent: Path) -> Path:
    """Copy a fixture genre pack (whole tree, incl. worlds/) into ``parent``.

    The clone keeps the fixture's directory basename (== ``slug``) because the
    loader fail-loud-validates ``lethality_policy.yaml``'s ``genre_key`` against
    the pack dir name. Each test owns a private ``tmp_path``, so reusing the
    slug as the basename never collides across tests.
    """
    dst = parent / slug
    shutil.copytree(fixture_pack_path(slug), dst)
    return dst


def _rewrite_yaml(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def _strip_def_beats(pack_dir: Path, category: str) -> str:
    """Set the (first) confrontation of ``category`` to zero beats; return its type."""
    found: dict[str, str] = {}

    def _mut(rules: dict) -> None:
        for conf in rules["confrontations"]:
            if conf.get("category") == category:
                conf["beats"] = []
                found["type"] = conf.get("type", "")
                return
        raise AssertionError(f"fixture has no {category!r} confrontation to strip")

    _rewrite_yaml(pack_dir / "rules.yaml", _mut)
    return found["type"]


def _empty_all_class_choices(pack_dir: Path) -> None:
    """Empty encounter_beat_choices on every class (the 108-3 end-state)."""

    def _mut(classes: list) -> None:
        for cls in classes:
            cls["encounter_beat_choices"] = []

    _rewrite_yaml(pack_dir / "classes.yaml", _mut)


def _empty_one_class_choices(pack_dir: Path, display_name: str) -> None:
    """Empty encounter_beat_choices on a single allowed class only."""
    hit: dict[str, bool] = {}

    def _mut(classes: list) -> None:
        for cls in classes:
            if cls.get("display_name") == display_name:
                cls["encounter_beat_choices"] = []
                hit["ok"] = True

    _rewrite_yaml(pack_dir / "classes.yaml", _mut)
    assert hit.get("ok"), f"fixture has no class named {display_name!r}"


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture watcher events the loader publishes (loader imports publish_event
    function-locally, so patching the hub module's attribute intercepts it)."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type: str, fields: dict, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "fields": fields, "kwargs": kwargs})

    monkeypatch.setattr(hub_mod, "publish_event", _capture)
    yield captured


# ---------------------------------------------------------------------------
# Relaxation 1 — beat-count gate moves to the loader, WN combat goes beatless
# ---------------------------------------------------------------------------


def test_denativized_wn_pack_loads(tmp_path: Path) -> None:
    """The real 108-3 end-state: a WWN pack whose combat def has ZERO beats and
    whose classes declare NO encounter_beat_choices loads cleanly (both
    relaxations fire together — stripping beats alone would orphan the class
    beat-choice refs at loader.py:~701, which is exactly why they ship as a
    pair). RED today: the model validator raises 'must have at least one beat'
    at parse before the loader is ever reached."""
    pack_dir = _clone_pack(WWN_TEST_PACK, tmp_path)
    _strip_def_beats(pack_dir, "combat")
    _empty_all_class_choices(pack_dir)

    pack = load_genre_pack(pack_dir)

    assert pack.rules.ruleset == "wwn"
    combat = [c for c in pack.rules.confrontations if c.category == "combat"]
    assert combat, "combat confrontation must survive the strip"
    assert combat[0].beats == [], "combat def must have loaded with zero beats"


def test_beatless_native_combat_def_still_raises(tmp_path: Path) -> None:
    """A NATIVE pack (ruleset defaults to 'native') with a zero-beat combat def
    must STILL fail loud — the relaxation is gated on the WN binding, not open
    to everyone. test_genre ships no classes.yaml, so this isolates the
    beat-count gate from the encounter_beat_choices coupling."""
    pack_dir = _clone_pack(NATIVE_TEST_PACK, tmp_path)
    _strip_def_beats(pack_dir, "combat")

    with pytest.raises(GenreError, match="at least one beat"):
        load_genre_pack(pack_dir)


def test_beatless_wn_social_def_still_raises(tmp_path: Path) -> None:
    """The relaxation is combat/hp_depletion ONLY. A non-combat (dial)
    confrontation keeps the native dial engine even under a WN binding (epic
    108), so a zero-beat *social* def must STILL raise even though the pack is
    WN. Class choices are emptied so the only possible failure path is the
    beat-count gate (not the encounter_beat_choices coupling). Guards against an
    over-broad 'WN ⇒ any beatless def is fine' implementation."""
    pack_dir = _clone_pack(WWN_TEST_PACK, tmp_path)
    _empty_all_class_choices(pack_dir)
    _strip_def_beats(pack_dir, "social")

    with pytest.raises(GenreError, match="at least one beat"):
        load_genre_pack(pack_dir)


def test_wn_combat_def_with_beats_still_loads(tmp_path: Path) -> None:
    """No-regression: a WN combat def that DOES author beats keeps loading and
    keeps its beats (the relaxation makes beats optional, not forbidden)."""
    pack_dir = _clone_pack(WWN_TEST_PACK, tmp_path)

    pack = load_genre_pack(pack_dir)

    combat = [c for c in pack.rules.confrontations if c.category == "combat"]
    assert combat and len(combat[0].beats) > 0


# ---------------------------------------------------------------------------
# Relaxation 2 — encounter_beat_choices↔combat-beats coupling goes WN-aware
# ---------------------------------------------------------------------------


def test_wn_class_empty_encounter_beat_choices_loads(tmp_path: Path) -> None:
    """A WN ``allowed_classes`` class may declare empty encounter_beat_choices
    (the WN engine owns the action set). Combat beats are left intact so this
    isolates the class-coupling relaxation from the beat-count one. RED today:
    loader.py:~696 raises PackError 'encounter_beat_choices is empty'."""
    pack_dir = _clone_pack(WWN_TEST_PACK, tmp_path)
    _empty_one_class_choices(pack_dir, "Warrior")

    pack = load_genre_pack(pack_dir)

    warrior = next(c for c in pack.classes if c.display_name == "Warrior")
    assert warrior.encounter_beat_choices == []
    assert "Warrior" in pack.rules.allowed_classes


def test_validate_class_filter_refs_native_still_raises_on_empty() -> None:
    """Native mirror for relaxation 2 (no native fixture ships classes.yaml, so
    we drive the validator directly with real WN objects re-tagged native via
    model_copy — model_copy skips re-validation, so the native tag sticks)."""
    pack = load_fixture_pack(WWN_TEST_PACK)
    native_rules = pack.rules.model_copy(update={"ruleset": "native"})
    classes = [
        c.model_copy(update={"encounter_beat_choices": []}) if c.display_name == "Warrior" else c
        for c in pack.classes
    ]

    with pytest.raises(PackError, match="encounter_beat_choices is empty"):
        _validate_class_filter_refs(native_rules, classes)


def test_validate_class_filter_refs_wn_allows_empty() -> None:
    """WN-gate unit twin: the same emptied class under the unchanged wwn ruleset
    must NOT raise. RED today: the empty-check at loader.py:~696 fires for every
    ruleset."""
    pack = load_fixture_pack(WWN_TEST_PACK)
    classes = [
        c.model_copy(update={"encounter_beat_choices": []}) if c.display_name == "Warrior" else c
        for c in pack.classes
    ]

    # Must return without raising; the WN binding owns the action set.
    _validate_class_filter_refs(pack.rules, classes)


# ---------------------------------------------------------------------------
# OTEL (AC5) — the beat-optional relaxation is a lie-detector surface
# ---------------------------------------------------------------------------


def test_denativized_wn_pack_emits_beat_optional_span(
    tmp_path: Path, captured_events: list[dict[str, Any]]
) -> None:
    """Allowing a beatless WN combat def MUST emit a ``state_transition`` watcher
    span (``field == "wn_beat_optional"``) carrying the ruleset + the relaxed
    confrontation_type, so the GM panel can prove the WN gate fired rather than
    the validation having been silently removed. RED today: the load raises
    before any such span is published."""
    pack_dir = _clone_pack(WWN_TEST_PACK, tmp_path)
    combat_type = _strip_def_beats(pack_dir, "combat")
    _empty_all_class_choices(pack_dir)

    load_genre_pack(pack_dir)

    relaxed = [
        e
        for e in captured_events
        if e["event_type"] == "state_transition" and e["fields"].get("field") == "wn_beat_optional"
    ]
    assert len(relaxed) >= 1, (
        "loader must publish a wn_beat_optional state_transition span when it "
        f"allows a beatless WN combat def; captured fields: "
        f"{[e['fields'].get('field') for e in captured_events]}"
    )
    fields = relaxed[0]["fields"]
    assert fields.get("ruleset") == "wwn"
    assert fields.get("confrontation_type") == combat_type
