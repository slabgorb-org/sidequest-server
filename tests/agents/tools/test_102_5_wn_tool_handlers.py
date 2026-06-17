"""RED tests for Story 102-5 (AC2 + AC3) — WN narrator tool handlers, tool-level.

§12 isolation mandate: every handler is testable WITHOUT a live narrator —
invoked here directly with typed payloads (valid AND invalid). Each handler is
a THIN wrapper (the commit_effort pattern): it resolves the actor from the
snapshot, routes to the bound RulesetModule surface, persists, and enriches
the dispatch span. No resolution math in the handler (AC2 — review-level; the
behavior pinned here is that module-scoped spans fire and module rules hold).

Contract pinned (TEA, 102-5 RED):

* ``wn_attack``: d20 + modifier vs target AC (the module's attack math); on a
  hit the weapon's damage dice resolve through the EXISTING damage seam
  (``resolve_damage_spec_from_beat_and_actor`` priority ladder — inventory
  item ``damage`` dict here) and the HpPool delta persists. A hit with NO
  resolvable damage spec is a loud error, never a silent 0-damage hit (No
  Silent Fallbacks; the seeded-weapon 86-1 lesson).
* ``wn_skill_check``: 2d6 + attribute mod + skill_level vs the pack's
  difficulty LADDER KEY (cfg.difficulties); unknown key fails loud.
* ``wn_save``: d20 + best-attribute mod vs the module's derived save target
  (save_base - (level-1)); the save CATEGORY vocabulary is module-owned
  ("luck" exists on wwn, not on swn — the family differentiates, the contract
  doesn't fork).
* ``wn_adjudicate_dead_premise``: NOTHING mechanical (§8 — no state mutation
  until the narrator routes a follow-up through wn_attack/etc.); records the
  redirect-vs-fizzle ruling + reason on its span.
* Slug honesty (epic invariant): module spans carry the BOUND module's slug —
  ``wwn.attack.resolved`` on a wwn pack, ``awn.attack.resolved`` on awn.
* Guards (the commit_effort pattern): unknown actor → NOT_FOUND; no active
  session → ERROR_FATAL; dial pack → loud error through dispatch.

All tests FAIL until 102-5 is implemented (KeyError on the unregistered tool).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import sidequest.agents.tools  # noqa: F401  (production registration path)
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import AwnConfig, CwnConfig, SwnConfig, WwnConfig

# ---------------------------------------------------------------------------
# Fixtures — minimal duck-typed packs per WN slug (the adjust_system_strain
# pattern), identity attribute map so stats keys match SWN attribute names.
# ---------------------------------------------------------------------------

_AMAP = {
    a: a for a in ("STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA")
}

_CFGS = {
    "swn": lambda: SwnConfig(attribute_map=_AMAP),
    "wwn": lambda: WwnConfig(attribute_map=_AMAP),
    "cwn": lambda: CwnConfig(attribute_map=_AMAP),
    "awn": lambda: AwnConfig(attribute_map=_AMAP),
}


@dataclass
class _FakeRules:
    ruleset: str = "wwn"
    _cfg: Any = None

    def __post_init__(self) -> None:
        if self._cfg is None and self.ruleset in _CFGS:
            self._cfg = _CFGS[self.ruleset]()

    def ruleset_config(self) -> Any:
        return self._cfg


@dataclass
class _FakePack:
    rules: _FakeRules = field(default_factory=_FakeRules)


def _pack(slug: str) -> _FakePack:
    return _FakePack(rules=_FakeRules(ruleset=slug))


_STATS = {a: 14 for a in _AMAP}

_WEAPON = {"id": "shard-knife", "name": "Shard Knife", "damage": {"dice": "1d6", "bonus": 0}}


def _pc(
    name: str,
    *,
    ac: int = 10,
    hp: int = 10,
    level: int = 1,
    items: list[dict] | None = None,
) -> Character:
    core = CreatureCore(
        name=name,
        description="A scarred foundry runner.",
        personality="wary",
        level=level,
        inventory=Inventory(items=items or []),
        hp=HpPool(current=hp, max=hp, base_max=hp),
        armor_class=ac,
    )
    return Character(
        core=core,
        backstory="Forged in the long foundry.",
        char_class="Warrior",
        race="Human",
        stats=dict(_STATS),
    )


def _dark_pc(name: str, *, level: int = 1) -> Character:
    """A PC carrying an in-the-dark Status (roll_modifier=-2) — the marquee
    light-and-darkness penalty. Same stats as _pc so the only difference in a
    resolved modifier is the status term."""
    pc = _pc(name, level=level)
    pc.core.statuses.append(
        Status(text="in the dark", severity=StatusSeverity.Wound, roll_modifier=-2)
    )
    return pc


def _snapshot(characters: list[Character]) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="long_foundry",
        turn_manager=TurnManager(interaction=1),
        characters=characters,
        npcs=[],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, genre_pack: Any, session_id: str = "s") -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Vesska",
        turn_number=3,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


async def _call(name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (§12 tool-level isolation)."""
    registered = default_registry._tools[name]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


async def _dispatch(name: str, arguments: dict, ctx: ToolContext):
    block = ToolUseBlock(id=f"t-{name}", name=name, arguments=arguments)
    return await default_registry.dispatch(block, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _span_names(otel_capture) -> list[str]:
    return [s.name for s in otel_capture.get_finished_spans()]


def _spans_named(otel_capture, name: str) -> list[dict]:
    return [dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == name]


# ===========================================================================
# wn_attack
# ===========================================================================


async def test_attack_forced_hit_applies_weapon_damage_to_hp_pool() -> None:
    """§8: d20 vs AC; on hit, damage dice → HpPool delta. AC -100 forces the
    hit deterministically; the Shard Knife's 1d6 lands on the target's pool."""
    attacker = _pc("Vesska", items=[dict(_WEAPON)])
    target = _pc("Husk", ac=-100, hp=10)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call(
        "wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "Shard Knife"}, ctx
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["hit"] is True
    assert 1 <= p["d20"] <= 20
    assert p["target_ac"] == -100
    assert p["attack_total"] == p["d20"] + p["modifier"]
    assert 1 <= p["damage"] <= 6, f"1d6 weapon damage out of range: {p['damage']}"

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current == 10 - p["damage"], (
        "the HpPool delta must persist (narrator describes; engine adjudicates)"
    )


async def test_attack_forced_miss_leaves_hp_untouched() -> None:
    """AC 100 forces the miss: no damage, no mutation, honest payload."""
    attacker = _pc("Vesska", items=[dict(_WEAPON)])
    target = _pc("Husk", ac=100, hp=10)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call(
        "wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "Shard Knife"}, ctx
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["hit"] is False
    assert p["damage"] == 0

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current == 10


async def test_attack_unknown_attacker_returns_not_found() -> None:
    target = _pc("Husk")
    store = _store_with(_snapshot([target]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call("wn_attack", {"attacker": "Ghost", "target": "Husk", "weapon": "x"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None and "Ghost" in r.message


async def test_attack_unknown_target_returns_not_found() -> None:
    attacker = _pc("Vesska", items=[dict(_WEAPON)])
    store = _store_with(_snapshot([attacker]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call("wn_attack", {"attacker": "Vesska", "target": "Ghost", "weapon": "x"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None and "Ghost" in r.message


async def test_attack_on_dial_pack_fails_loud_through_dispatch() -> None:
    """The per-tool self-guard backstop (73-15 AC-3 pattern): even if the
    advertisement filter regresses, dispatching on a dial pack is a loud
    error ToolResult, never a silent generic resolution."""
    attacker = _pc("Vesska", items=[dict(_WEAPON)])
    target = _pc("Husk", ac=-100)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_FakePack(rules=_FakeRules(ruleset="dial", _cfg=object())))

    out = await _dispatch(
        "wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "Shard Knife"}, ctx
    )
    assert out.is_error is True
    assert "dial" in out.content.lower() or "ruleset" in out.content.lower()


async def test_attack_hit_with_no_resolvable_damage_fails_loud_not_zero() -> None:
    """No Silent Fallbacks: an unarmed attacker on a pack with no unarmed
    floor cannot produce a convincing 0-damage 'hit' — the 86-1 seeded-weapon
    lesson. The narrator must get a loud, recoverable error to relay."""
    attacker = _pc("Vesska", items=[])  # no weapon, fake pack has no unarmed_damage
    target = _pc("Husk", ac=-100, hp=10)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    out = await _dispatch(
        "wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "fists"}, ctx
    )
    assert out.is_error is True, (
        "a hit with no resolvable damage spec must be a loud error, not a 0-HP no-op"
    )
    assert "damage" in out.content.lower()

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current == 10


async def test_attack_resolves_the_NAMED_weapon_not_the_first_in_inventory() -> None:
    """Regression (102-5 review R-1): the ``weapon`` arg must select the damage
    source. A multi-weapon actor carries a feeble Dart (1d2 → max 2) FIRST and a
    Maul (3d6+3 → min 6) SECOND. Naming the Maul must roll the Maul, not the
    first damage-bearing item — otherwise the narrator's prose ("I swing the
    maul") diverges from the engine's dice, the exact improv this system exists
    to prevent."""
    dart = {"id": "dart", "name": "Dart", "damage": {"dice": "1d2", "bonus": 0}}
    maul = {"id": "maul", "name": "Maul", "damage": {"dice": "3d6", "bonus": 3}}
    attacker = _pc("Vesska", items=[dict(dart), dict(maul)])
    target = _pc("Husk", ac=-100, hp=40)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call("wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "Maul"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["hit"] is True
    assert 6 <= p["damage"] <= 21, (
        f"named Maul (3d6+3, min 6) but got {p['damage']} — the Dart (max 2) was used"
    )

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current == 40 - p["damage"]


async def test_attack_naming_a_weapon_the_actor_does_not_carry_fails_loud() -> None:
    """The named weapon must be CARRIED. Naming a weapon absent from inventory
    (and not an unarmed strike) is a loud error, not a silent fall-through to
    some other item's dice (No Silent Fallbacks)."""
    attacker = _pc("Vesska", items=[{"id": "dart", "name": "Dart", "damage": {"dice": "1d2"}}])
    target = _pc("Husk", ac=-100, hp=10)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    out = await _dispatch(
        "wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "Plasma Lance"}, ctx
    )
    assert out.is_error is True
    assert "plasma lance" in out.content.lower() or "damage" in out.content.lower()

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current == 10


@pytest.mark.parametrize("slug", ["wwn", "awn"])
async def test_attack_emits_module_span_with_bound_slug(slug: str, otel_capture) -> None:
    """AC2 + the slug-honesty invariant: the module-scoped resolution span is
    prefixed with the BOUND module's slug (awn says awn, not cwn), alongside
    the registry's by-construction tool.{category}.wn_attack span."""
    attacker = _pc("Vesska", items=[dict(_WEAPON)])
    target = _pc("Husk", ac=-100)
    store = _store_with(_snapshot([attacker, target]))
    ctx = _make_ctx(store, genre_pack=_pack(slug), session_id=f"span-{slug}")

    out = await _dispatch(
        "wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "Shard Knife"}, ctx
    )
    assert out.is_error is False

    names = _span_names(otel_capture)
    assert any(re.fullmatch(r"tool\.(read|write|gen)\.wn_attack", n) for n in names), (
        f"registry dispatch span missing for wn_attack; got {names}"
    )
    resolution = _spans_named(otel_capture, f"{slug}.attack.resolved")
    assert resolution, f"no {slug}.attack.resolved span — the GM panel cannot see the attack"
    attrs = resolution[0]
    assert attrs.get("actor") == "Vesska"
    assert attrs.get("target") == "Husk"
    assert attrs.get("hit") is True


# ===========================================================================
# wn_skill_check
# ===========================================================================


async def test_skill_check_resolves_2d6_against_the_ladder() -> None:
    """§8: 2d6 + attribute mod + skill_level vs the difficulty ladder.
    'hard' resolves to 12 on the stock SWN-family ladder; the payload's
    success/margin must be internally consistent with the resolved numbers."""
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call(
        "wn_skill_check",
        {
            "actor": "Vesska",
            "skill": "Sneak",
            "attribute": "DEXTERITY",
            "skill_level": 1,
            "difficulty": "hard",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["difficulty"] == 12, "stock ladder: hard=12 (cfg.difficulties, never a literal)"
    assert p["success"] == (p["total"] >= 12)
    assert p["margin"] == p["total"] - 12


async def test_skill_check_unknown_difficulty_key_fails_loud() -> None:
    """The ladder is authored config — an unknown key is a loud error through
    dispatch, never a silently-invented DC (server is the only DC author)."""
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    out = await _dispatch(
        "wn_skill_check",
        {
            "actor": "Vesska",
            "skill": "Sneak",
            "attribute": "DEXTERITY",
            "skill_level": 1,
            "difficulty": "impossible",
        },
        ctx,
    )
    assert out.is_error is True
    assert "impossible" in out.content, (
        f"the error must name the unknown ladder key, not a generic failure: {out.content}"
    )


async def test_skill_check_out_of_range_skill_level_rejected_by_schema() -> None:
    """SWN skill levels run −1..+4 — the typed payload rejects 9 at the
    validation layer (AC3: invalid → typed, loud)."""
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    out = await _dispatch(
        "wn_skill_check",
        {
            "actor": "Vesska",
            "skill": "Sneak",
            "attribute": "DEXTERITY",
            "skill_level": 9,
            "difficulty": "hard",
        },
        ctx,
    )
    assert out.is_error is True
    assert "validation" in out.content.lower()


async def test_skill_check_emits_module_span(otel_capture) -> None:
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"), session_id="span-check")

    out = await _dispatch(
        "wn_skill_check",
        {
            "actor": "Vesska",
            "skill": "Sneak",
            "attribute": "DEXTERITY",
            "skill_level": 0,
            "difficulty": "easy",
        },
        ctx,
    )
    assert out.is_error is False
    resolution = _spans_named(otel_capture, "wwn.skill_check.resolved")
    assert resolution, "no wwn.skill_check.resolved span — the check is invisible to the GM panel"
    assert resolution[0].get("actor") == "Vesska"


# ===========================================================================
# wn_save
# ===========================================================================


async def test_save_resolves_d20_against_derived_target() -> None:
    """§8: d20 vs the SWN save derivation (save_base 15, −1/level → 13 at
    level 3). Payload consistency: success == (d20 + modifier >= target)."""
    actor = _pc("Vesska", level=3)
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call(
        "wn_save",
        {"actor": "Vesska", "save": "physical", "effect": "shrapnel burst"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["target"] == 13, "save_base 15 - (level 3 - 1) = 13 — derived, never improvised"
    assert 1 <= p["d20"] <= 20
    assert p["success"] == (p["d20"] + p["modifier"] >= 13)
    assert p["save"] == "physical"


async def test_save_category_vocabulary_is_module_owned() -> None:
    """'luck' is a WWN save, not an SWN one — one contract, module-owned
    vocabulary. The same call succeeds on wwn and fails loud on swn."""
    actor = _pc("Vesska")

    wwn_store = _store_with(_snapshot([actor]))
    wwn_ctx = _make_ctx(wwn_store, genre_pack=_pack("wwn"), session_id="save-wwn")
    r = await _call(
        "wn_save", {"actor": "Vesska", "save": "luck", "effect": "stray ricochet"}, wwn_ctx
    )
    assert r.status is ToolResultStatus.OK

    swn_store = _store_with(_snapshot([_pc("Vesska")]))
    swn_ctx = _make_ctx(swn_store, genre_pack=_pack("swn"), session_id="save-swn")
    out = await _dispatch(
        "wn_save", {"actor": "Vesska", "save": "luck", "effect": "stray ricochet"}, swn_ctx
    )
    assert out.is_error is True
    assert "luck" in out.content.lower()


async def test_save_unknown_category_fails_loud() -> None:
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    out = await _dispatch("wn_save", {"actor": "Vesska", "save": "vibes", "effect": "x"}, ctx)
    assert out.is_error is True
    assert "vibes" in out.content, (
        f"the error must name the unknown save category, not a generic failure: {out.content}"
    )


async def test_save_emits_module_span(otel_capture) -> None:
    actor = _pc("Vesska", level=2)
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"), session_id="span-save")

    out = await _dispatch(
        "wn_save", {"actor": "Vesska", "save": "mental", "effect": "psychic static"}, ctx
    )
    assert out.is_error is False
    resolution = _spans_named(otel_capture, "wwn.save.resolved")
    assert resolution, "no wwn.save.resolved span — the save is invisible to the GM panel"
    assert resolution[0].get("actor") == "Vesska"
    assert resolution[0].get("save") == "mental"


# ===========================================================================
# Light & darkness: the status roll_modifier must reach the NARRATOR path
# (wn_skill_check / wn_save), not just the player-initiated check_throw handler.
# The whole point of the feature is the dark is undodgeable — the failed
# find-the-rope-back search/save kills by degrees regardless of who triggers it.
# ===========================================================================


async def test_wn_skill_check_applies_darkness_penalty() -> None:
    """A narrator-adjudicated search in the dark gets the -2 status penalty.
    Non-vacuous: the dark actor's resolved modifier is exactly 2 below the
    clean actor's, with identical stats/skill/attribute."""
    clean_store = _store_with(_snapshot([_pc("Vesska")]))
    clean_ctx = _make_ctx(clean_store, genre_pack=_pack("wwn"), session_id="dark-check-clean")
    args = {
        "actor": "Vesska",
        "skill": "Notice",
        "attribute": "WISDOM",
        "skill_level": 1,
        "difficulty": "hard",
    }
    clean = _payload(await _call("wn_skill_check", args, clean_ctx))

    dark_store = _store_with(_snapshot([_dark_pc("Vesska")]))
    dark_ctx = _make_ctx(dark_store, genre_pack=_pack("wwn"), session_id="dark-check-dark")
    dark = _payload(await _call("wn_skill_check", args, dark_ctx))

    assert dark["modifier"] == clean["modifier"] - 2, (
        "the in-the-dark status (-2) must reach the narrator-driven skill check, "
        "not only the player-initiated check_throw path"
    )


async def test_wn_save_applies_darkness_penalty() -> None:
    """A narrator-adjudicated save in the dark gets the -2 status penalty on the
    d20 modifier (same derivation, only the status term differs)."""
    clean_store = _store_with(_snapshot([_pc("Vesska", level=3)]))
    clean_ctx = _make_ctx(clean_store, genre_pack=_pack("wwn"), session_id="dark-save-clean")
    args = {"actor": "Vesska", "save": "physical", "effect": "the long fall in the dark"}
    clean = _payload(await _call("wn_save", args, clean_ctx))

    dark_store = _store_with(_snapshot([_dark_pc("Vesska", level=3)]))
    dark_ctx = _make_ctx(dark_store, genre_pack=_pack("wwn"), session_id="dark-save-dark")
    dark = _payload(await _call("wn_save", args, dark_ctx))

    assert dark["modifier"] == clean["modifier"] - 2, (
        "the in-the-dark status (-2) must reach the narrator-driven save"
    )


# ===========================================================================
# wn_adjudicate_dead_premise
# ===========================================================================


async def test_dead_premise_adjudication_mutates_nothing() -> None:
    """§8: 'nothing mechanical until the narrator picks a landing' — the
    adjudication records the ruling; any redirect routes through wn_attack as
    a SEPARATE call. HP and pools are untouched."""
    actor = _pc("Vesska", items=[dict(_WEAPON)])
    bystander = _pc("Husk", hp=10)
    store = _store_with(_snapshot([actor, bystander]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    r = await _call(
        "wn_adjudicate_dead_premise",
        {
            "actor": "Vesska",
            "action": "hurl the shard-knife at the rust-priest",
            "gone_target": "rust-priest",
            "ruling": "redirect",
            "reason": "the blade is already in the air when the priest drops",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["ruling"] == "redirect"

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Husk")
    assert core is not None
    assert core.hp.current == 10, "dead-premise adjudication must not deal damage itself"


async def test_dead_premise_ruling_outside_enum_rejected() -> None:
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"))

    out = await _dispatch(
        "wn_adjudicate_dead_premise",
        {
            "actor": "Vesska",
            "action": "x",
            "gone_target": "y",
            "ruling": "reroll",
            "reason": "z",
        },
        ctx,
    )
    assert out.is_error is True
    assert "validation" in out.content.lower()


async def test_dead_premise_span_records_ruling_and_reason(otel_capture) -> None:
    """§8 OTEL column: 'span recording redirect-vs-fizzle and why'."""
    actor = _pc("Vesska")
    store = _store_with(_snapshot([actor]))
    ctx = _make_ctx(store, genre_pack=_pack("wwn"), session_id="span-dp")

    out = await _dispatch(
        "wn_adjudicate_dead_premise",
        {
            "actor": "Vesska",
            "action": "fire at the husk",
            "gone_target": "husk-3",
            "ruling": "fizzle",
            "reason": "no plausible second target in the kill-zone",
        },
        ctx,
    )
    assert out.is_error is False
    spans = _spans_named(otel_capture, "wwn.dead_premise.adjudicated")
    assert spans, "no wwn.dead_premise.adjudicated span — the ruling is invisible"
    attrs = spans[0]
    assert attrs.get("ruling") == "fizzle"
    assert attrs.get("reason") == "no plausible second target in the kill-zone"
    assert attrs.get("actor") == "Vesska"


# ===========================================================================
# Common guard: no active session is fatal (every WRITE-shaped WN tool)
# ===========================================================================


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("wn_attack", {"attacker": "Vesska", "target": "Husk", "weapon": "x"}),
        (
            "wn_skill_check",
            {
                "actor": "Vesska",
                "skill": "Sneak",
                "attribute": "DEXTERITY",
                "skill_level": 0,
                "difficulty": "easy",
            },
        ),
        ("wn_save", {"actor": "Vesska", "save": "physical", "effect": "x"}),
        (
            "wn_adjudicate_dead_premise",
            {
                "actor": "Vesska",
                "action": "a",
                "gone_target": "g",
                "ruling": "fizzle",
                "reason": "r",
            },
        ),
    ],
)
async def test_no_active_session_is_fatal(name: str, arguments: dict) -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store(slug=f"wn-empty-{name}")
    ctx = _make_ctx(store, genre_pack=_pack("wwn"), session_id=f"empty-{name}")

    r = await _call(name, arguments, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None and "no active session" in r.message
