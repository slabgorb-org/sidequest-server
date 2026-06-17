"""Tests for fail-loud ruleset slug enforcement (spec 2026-06-17 §1, No Silent Fallbacks).

All three silent-default sites must raise instead of returning a "dial" / "native"
default when pack/rules are absent.  The canonical shape is:
  - instantiate_table_encounter: ruleset_slug is a required parameter (no default).
  - pregen.seed_manual: raises ValueError when the pack loaded successfully but
    pack.rules is None (a configuration error); a pack that fails to load entirely
    degrades gracefully per ADR-006 (logs pack_load_failed, keeps minting NPCs,
    does NOT raise).
  - encounter_lifecycle call site: raises ValueError via _raise_missing_ruleset.
"""

import inspect
import json
import random
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from sidequest.server.dispatch import pregen
from sidequest.server.dispatch.encounter_lifecycle import instantiate_table_encounter


def test_table_encounter_requires_explicit_ruleset_slug():
    """ruleset_slug is now required (no 'dial' default).

    Omitting it is a TypeError at the call boundary — the param has no default.
    """
    sig = inspect.signature(instantiate_table_encounter)
    assert sig.parameters["ruleset_slug"].default is inspect.Parameter.empty, (
        "ruleset_slug must have no default — a missing ruleset is a config error, "
        "not a silent 'dial' default (spec 2026-06-17 §1, No Silent Fallbacks)"
    )


def _stub_pack_no_rules() -> object:
    """A pack that loads successfully but has rules=None — a configuration error."""
    from sidequest.genre.models.archetype_constraints import (
        ArchetypeConstraints,
        GenreFlavor,
        ValidPairings,
    )

    constraints = ArchetypeConstraints(
        genre_flavor=GenreFlavor(),
        valid_pairings=ValidPairings(common=[], uncommon=[], rare=[], forbidden=[]),
        npc_roles_available=[],
    )
    culture_objs = [SimpleNamespace(name="Scrapborn")]
    spawnable = SimpleNamespace(name="Drifter", named_individual=False)
    pack = SimpleNamespace(
        cultures=culture_objs,
        archetype_constraints=constraints,
        rules=None,
    )
    pack.effective_cultures = lambda _world: (culture_objs, "stub")
    pack.effective_archetypes = lambda _world: ([spawnable], "stub")
    return pack


def test_seed_manual_raises_when_pack_loaded_but_rules_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """seed_manual raises ValueError when pack loaded OK but pack.rules is None.

    This is a configuration error (the pack YAML is missing a rules block), not
    a load failure.  ADR-006 graceful degradation does NOT apply here — a pack
    that loads but has no rules is broken config and must fail loud.
    """
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack_no_rules())
    monkeypatch.setattr(
        pregen,
        "namegen_main",
        lambda _argv: print(json.dumps({"name": "X", "role": "r", "culture": "c"})) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    from sidequest.game.monster_manual import MonsterManual

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="g", world="w")
        with pytest.raises(ValueError, match="pack/rules missing"):
            pregen.seed_manual(
                genre_packs_path=tmp_path / "packs",
                genre="g",
                world="w",
                manual=manual,
                rng=random.Random(0),
            )


def test_raise_missing_ruleset_helper_raises_valueerror() -> None:
    """_raise_missing_ruleset raises ValueError containing the context string."""
    from sidequest.server.dispatch.encounter_lifecycle import _raise_missing_ruleset

    with pytest.raises(ValueError, match="table_resolution"):
        _raise_missing_ruleset("table_resolution")
