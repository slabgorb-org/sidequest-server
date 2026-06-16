"""Story 122-2 — relocate pure combat-rules helpers below the server tier.

ADR-147 (Honest Layering) Move #1: ``find_confrontation_def`` and
``resolve_damage_spec_from_beat_and_actor`` are *pure combat-rules logic* that
"happen to live under ``server.dispatch`` by historical accident" (the admission
is in ``native.py``'s own layer-inversion comment). They force the game tier to
import **upward** into ``server/`` — ``native.py`` does it at module level,
``without_number.py`` dodges the resulting circular-import with lazy in-method
imports. This story moves them down into the game tier and deletes those
upward edges.

Test layers:

* **Layering (RED → GREEN):** ``native.py`` and ``without_number.py`` must stop
  importing anything from ``sidequest.server``. These FAIL today (the imports
  are live) and pass once Dev relocates the helpers. AST-based, so they survive
  whatever module Dev chooses as the new home (ADR-147: "Dev confirms the exact
  module") and catch the lazy in-method imports too (``ast.walk`` descends into
  function bodies).

* **Transitive-dependency guard (GREEN, stays GREEN):**
  ``resolve_damage_spec_from_beat_and_actor`` pulls in ``resolve_inventory``
  from ``server.dispatch.inventory_resolve`` (priority-3 catalog lookup).
  ``resolve_inventory`` is itself pure (imports only ``genre`` + ``telemetry``),
  so ADR-147 Move #1 — "and any sibling pure-resolution helpers they pull in" —
  puts it in scope. If Dev relocates the damage helper into the game tier but
  leaves it importing ``resolve_inventory`` from ``server``, the violation has
  merely *moved*, not gone. This guard scans the whole game tier so it catches
  that regardless of the new module's name.

* **Behavior characterization (GREEN, stays GREEN):** both helpers are exercised
  through their real consumers — ``NativeRulesetModule`` and a Without-Number
  sibling — so the relocation cannot silently change combat resolution and the
  wiring (consumer → relocated helper) is proven end-to-end on both touched
  modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.native import NativeRulesetModule
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import BeatDef, RulesConfig

# Repo-root-relative anchor for the package under test.
_SIDEQUEST_ROOT = Path(__file__).resolve().parents[3] / "sidequest"
_GAME_TIER_DIRS = ("game", "genre", "orbital", "magic", "interior")


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _server_imports(module_path: Path) -> list[str]:
    """Return every ``sidequest.server*`` import target in ``module_path``.

    Walks the full AST (so lazy in-method imports are included) and collects
    both ``from sidequest.server... import X`` and ``import sidequest.server...``
    forms. Each entry is rendered ``"<source_module>:<name>"`` for a legible
    failure message.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "sidequest.server" or mod.startswith("sidequest.server."):
                for alias in node.names:
                    hits.append(f"{mod}:{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sidequest.server" or alias.name.startswith("sidequest.server."):
                    hits.append(f"import {alias.name}")
    return hits


def _imports_target_module(module_path: Path, target_module: str) -> bool:
    """True if ``module_path`` imports from ``target_module`` (or a submodule)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == target_module or mod.startswith(target_module + "."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module or alias.name.startswith(target_module + "."):
                    return True
    return False


def _game_tier_py_files() -> list[Path]:
    files: list[Path] = []
    for tier in _GAME_TIER_DIRS:
        files.extend((_SIDEQUEST_ROOT / tier).rglob("*.py"))
    return files


# ---------------------------------------------------------------------------
# Layering — the upward edges this story deletes (RED until the move lands)
# ---------------------------------------------------------------------------


def test_native_module_imports_nothing_from_server():
    """native.py must not import upward into sidequest.server (ADR-147 law).

    Today its only server imports ARE the two helpers being relocated
    (lines 21-22), so this is the precise RED marker for 122-2's native edge.
    """
    native_path = _SIDEQUEST_ROOT / "game" / "ruleset" / "native.py"
    offenders = _server_imports(native_path)
    assert offenders == [], (
        "game/ruleset/native.py still imports upward into sidequest.server "
        f"(ADR-147 forbids game->server): {offenders}"
    )


def test_without_number_module_imports_nothing_from_server():
    """without_number.py must not import upward into sidequest.server.

    Its server imports today are the two *lazy* in-method ones (lines 141, 275);
    ast.walk descends into the method bodies, so they are caught. The relocation
    must also delete these lazy-import workarounds (ADR-147 Move #1).
    """
    wn_path = _SIDEQUEST_ROOT / "game" / "ruleset" / "without_number.py"
    offenders = _server_imports(wn_path)
    assert offenders == [], (
        "game/ruleset/without_number.py still imports upward into sidequest.server "
        f"(lazy or otherwise; ADR-147 forbids game->server): {offenders}"
    )


# ---------------------------------------------------------------------------
# Transitive-dependency guard — resolve_inventory must come along (GREEN now)
# ---------------------------------------------------------------------------


def test_no_game_tier_module_imports_inventory_resolve_from_server():
    """The relocated damage helper's own dependency must not re-open the edge.

    ``resolve_damage_spec_from_beat_and_actor`` pulls ``resolve_inventory`` from
    ``server.dispatch.inventory_resolve`` (priority-3 catalog lookup).
    ``resolve_inventory`` is pure, so ADR-147 Move #1 relocates it alongside the
    damage helper. No game-tier module imports it from server today (the only
    game-tier mentions are in comments). This guard goes RED if Dev moves the
    damage helper down but leaves it reaching back up for ``resolve_inventory`` —
    catching a relocated-but-not-fixed violation regardless of the new module's
    name.
    """
    target = "sidequest.server.dispatch.inventory_resolve"
    offenders = [
        str(p.relative_to(_SIDEQUEST_ROOT))
        for p in _game_tier_py_files()
        if _imports_target_module(p, target)
    ]
    assert offenders == [], (
        f"game-tier modules import {target} (upward edge); resolve_inventory must "
        f"relocate with the damage helper (ADR-147 Move #1): {offenders}"
    )


# ---------------------------------------------------------------------------
# Behavior characterization — find_confrontation_def via the ruleset seam
# ---------------------------------------------------------------------------


class _FakeConfrontationDef:
    """Minimal stand-in: find_confrontation_def only reads ``confrontation_type``."""

    def __init__(self, confrontation_type: str, label: str):
        self.confrontation_type = confrontation_type
        self.label = label


@pytest.mark.parametrize("module_name", ["native", "wwn"])
def test_find_confrontation_returns_exact_match(module_name: str):
    """Both touched consumers resolve a def by exact confrontation_type match."""
    module = get_ruleset_module(module_name)
    defs = [
        _FakeConfrontationDef("negotiation", "Parley"),
        _FakeConfrontationDef("duel", "Sword Duel"),
    ]
    result = module.find_confrontation(defs, "duel")
    assert result is not None
    assert result.confrontation_type == "duel"
    assert result.label == "Sword Duel"


@pytest.mark.parametrize("module_name", ["native", "wwn"])
def test_find_confrontation_returns_none_on_miss(module_name: str):
    """No matching confrontation_type returns None (caller handles the miss)."""
    module = get_ruleset_module(module_name)
    defs = [_FakeConfrontationDef("negotiation", "Parley")]
    assert module.find_confrontation(defs, "duel") is None


def test_find_confrontation_returns_first_match_on_duplicate_type():
    """The iteration order contract: the FIRST def whose type matches wins."""
    first = _FakeConfrontationDef("duel", "First")
    second = _FakeConfrontationDef("duel", "Second")
    result = NativeRulesetModule().find_confrontation([first, second], "duel")
    assert result is first
    assert result.label == "First"


def test_find_confrontation_empty_list_returns_none():
    assert NativeRulesetModule().find_confrontation([], "duel") is None


# ---------------------------------------------------------------------------
# Behavior characterization — resolve_damage_spec via the ruleset seam
# ---------------------------------------------------------------------------


def _strike_beat(damage_override: DamageSpec | None = None) -> BeatDef:
    payload: dict = {
        "id": "strike",
        "label": "Strike",
        "kind": "strike",
        "base": 2,
        "stat_check": "STRENGTH",
        "damage_channel": "strike",
    }
    if damage_override is not None:
        payload["damage_override"] = damage_override.model_dump()
    return BeatDef.model_validate(payload)


def _pack_with_unarmed(unarmed: DamageSpec | None):
    from unittest.mock import MagicMock

    pack = MagicMock()
    pack.rules = RulesConfig(unarmed_damage=unarmed)
    pack.inventory = None
    return pack


def test_resolve_damage_beat_override_wins_priority1():
    """Priority 1: an explicit beat damage_override is returned verbatim."""
    beat = _strike_beat(damage_override=DamageSpec(dice="2d6", bonus=1))
    actor = CreatureCore(
        name="Brawler",
        description="d",
        personality="p",
        inventory=Inventory(items=[{"id": "longsword", "damage": "1d8"}]),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    pack = _pack_with_unarmed(DamageSpec(dice="1d4", bonus=0))
    spec = NativeRulesetModule().resolve_damage(beat=beat, actor_core=actor, pack=pack)
    assert spec is not None
    assert spec.dice == "2d6"
    assert spec.bonus == 1


def test_resolve_damage_equipped_weapon_beats_unarmed_priority2():
    """Priority 2: an inventory item's damage dict wins over the unarmed floor."""
    beat = _strike_beat()
    actor = CreatureCore(
        name="Brawler",
        description="d",
        personality="p",
        inventory=Inventory(items=[{"id": "longsword", "damage": "1d8"}]),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    pack = _pack_with_unarmed(DamageSpec(dice="1d4", bonus=0))
    spec = NativeRulesetModule().resolve_damage(beat=beat, actor_core=actor, pack=pack)
    assert spec is not None
    assert spec.dice == "1d8"


def test_resolve_damage_unarmed_floor_when_empty_handed_priority4():
    """Priority 4: an empty-handed actor floors to pack.rules.unarmed_damage."""
    beat = _strike_beat()
    actor = CreatureCore(
        name="Brawler",
        description="d",
        personality="p",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    pack = _pack_with_unarmed(DamageSpec(dice="1d4", bonus=0))
    spec = NativeRulesetModule().resolve_damage(beat=beat, actor_core=actor, pack=pack)
    assert spec is not None
    assert spec.dice == "1d4"


def test_resolve_damage_returns_none_when_nothing_resolves():
    """No override, no weapon, no unarmed floor ⇒ None (caller logs + skips)."""
    beat = _strike_beat()
    actor = CreatureCore(
        name="Brawler",
        description="d",
        personality="p",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    pack = _pack_with_unarmed(None)
    spec = NativeRulesetModule().resolve_damage(beat=beat, actor_core=actor, pack=pack)
    assert spec is None
