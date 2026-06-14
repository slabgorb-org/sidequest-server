"""WN narrator tool contract (Story 102-5, SWN module design §8 / ADR-117).

ONE family contract, four "Without Number" modules. The narrator drives WN
mechanical resolution through four tools — attack, skill check, save, and
dead-premise adjudication — and the BOUND module (swn/wwn/cwn/awn) does the
math behind the ADR-117 seam. The tools are advertised to every WN-family slug
and hidden from native packs (the family-ruleset declaration on the Registry).

Each handler is a THIN wrapper (the commit_effort pattern): resolve the actor
from the snapshot, route to the bound RulesetModule surface (``resolve_opponent_attack``
/ ``check_params`` / ``save_params``), persist any HP delta, and emit the
slug-honest module span (``{slug}.attack.resolved`` etc.) the GM panel reads.
No resolution math lives here — that is the module's job behind the seam.

Guards (fail loud — No Silent Fallbacks per CLAUDE.md):
- no active session → ERROR_FATAL
- pack ruleset not WN-family → ValueError (the per-tool self-guard backstop;
  the advertisement filter already hides these from a native narrator)
- unknown actor/target → NOT_FOUND
- a hit with no resolvable weapon damage → loud error, never a silent 0-damage hit
  (the 86-1 seeded-weapon lesson)
- unknown difficulty ladder key / save category → loud error naming the key
"""

from __future__ import annotations

import random
from typing import Any, Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.base import RulesetModule
from sidequest.genre.models.inventory import DamageSpec
from sidequest.telemetry.spans.wn import (
    WN_FAMILY_SLUGS,
    wn_attack_resolved_span,
    wn_dead_premise_adjudicated_span,
    wn_save_resolved_span,
    wn_skill_check_resolved_span,
)

# Module-level dice RNG (a real ``random.Random`` instance — ``DamageSpec.roll``
# and the d20/2d6 throws below take an instance, not the ``random`` module).
# Server-side rolls, seeded from OS entropy; spectator replay uses its own seed.
_RNG = random.Random()

# ---------------------------------------------------------------------------
# Shared resolution helpers
# ---------------------------------------------------------------------------


def _require_wn(ctx: ToolContext) -> tuple[RulesetModule, Any, str]:
    """Resolve ``(module, cfg, slug)`` for the bound WN pack; fail loud otherwise.

    The per-tool self-guard backstop (73-15 AC-3): even if the advertisement
    filter regressed and a native narrator reached this tool, dispatching it is
    a loud error, never a silent generic resolution. ``slug`` is the BOUND
    module's slug (``module.slug``) so every span is slug-honest — an awn pack
    says ``awn``, not ``cwn``.
    """
    rules = getattr(ctx.genre_pack, "rules", None)
    declared = getattr(rules, "ruleset", None)
    if rules is None or declared not in WN_FAMILY_SLUGS:
        raise ValueError(
            f"WN tool requires a WN-family ruleset (one of {WN_FAMILY_SLUGS}); "
            f"loaded pack has ruleset={declared!r}"
        )
    module = get_ruleset_module(declared)
    cfg = rules.ruleset_config()
    if cfg is None:
        raise ValueError(f"WN ruleset {declared!r} resolved no ruleset config")
    return module, cfg, module.slug


def _resolve_actor(snapshot: Any, name: str) -> tuple[CreatureCore, dict[str, int], int] | None:
    """Resolve ``(core, stats, level)`` for a named PC/NPC, or None.

    Ability scores live on ``Character.stats`` (CreatureCore carries none — the
    same resolution dispatch's downed seam uses). WN tool actors are PCs; an
    NPC without a stat block resolves an empty map and any stat read fails loud
    downstream rather than silently defaulting.
    """
    for ch in snapshot.characters:
        if ch.core.name == name:
            return ch.core, ch.stats, int(ch.core.level)
    for npc in snapshot.npcs:
        if npc.core.name == name:
            return npc.core, {}, int(npc.core.level)
    return None


# Narrator vocabulary for an unarmed strike — these resolve to the genre's
# unarmed-damage floor rather than an inventory weapon.
_UNARMED_TERMS = frozenset({"fists", "fist", "unarmed", "unarmed strike", "hands", "bare hands"})


def _damage_from_item(item: dict) -> DamageSpec | None:
    """The inline ``damage`` spec on an inventory item dict, or None.

    Mirrors priority 2 of ``resolve_damage_spec_from_beat_and_actor`` but for ONE
    named item (a dict with ``damage`` as an NdM string or a DamageSpec dict).
    Unparseable specs return None (the caller fails loud) — never a fabricated zero.
    """
    raw = item.get("damage")
    if isinstance(raw, dict):
        try:
            return DamageSpec.model_validate(raw)
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            return DamageSpec.model_validate({"dice": raw})
        except Exception:
            return None
    return None


def _resolve_weapon_damage(
    *, actor_core: CreatureCore, weapon_name: str, pack: Any, world_slug: str | None
) -> DamageSpec | None:
    """Resolve the DamageSpec for the *named* weapon (102-5 review R-1).

    The narrator names a weapon; the engine must roll THAT weapon's dice, not
    "whichever inventory item happens to carry a damage dict first" (the shared
    ``resolve_damage_spec_from_beat_and_actor`` seam matches by no name). For a
    multi-weapon actor that distinction is the difference between the narrator's
    prose and the engine's dice agreeing or diverging.

    Resolution (No Silent Fallbacks — never a fabricated zero, never the wrong
    weapon):
      1. The named weapon CARRIED with an inline ``damage`` dict → that spec.
      2. The named weapon carried but damage lives in the world/genre item
         catalog (real ``GenrePack`` only) → ``CatalogItem.damage`` by the item's
         id, looked up in the world-resolved catalog (ADR-145 D3 union: the genre
         SRD baseline is non-droppable, the world catalog merges over it by id, so
         a baseline weapon survives even when the world ships its own gear; only
         currency/kit replace wholesale).
      3. The named weapon NOT carried but it is an unarmed strike (``fists`` etc.)
         → the genre unarmed-strike floor (``pack.rules.unarmed_damage``).
      4. Otherwise (weapon not carried and not unarmed, or carried with no
         resolvable damage) → None; the caller fails loud.
    """
    from sidequest.genre.models.pack import GenrePack

    items = list(getattr(getattr(actor_core, "inventory", None), "items", []) or [])
    wanted = weapon_name.strip().lower()
    matched = next(
        (
            it
            for it in items
            if wanted
            in {
                str(it.get("name", "")).strip().lower(),
                str(it.get("id", "")).strip().lower(),
            }
        ),
        None,
    )

    if matched is not None:
        # Priority 1: inline damage on the named item.
        spec = _damage_from_item(matched)
        if spec is not None:
            return spec
        # Priority 2: world/genre catalog lookup by the named item's id. The
        # catalog tier requires a real GenrePack — a duck-typed pack carries no
        # catalog, so we fail loud rather than silently degrade (security WN-1).
        item_id = matched.get("id")
        if item_id and isinstance(pack, GenrePack):
            from sidequest.server.dispatch.inventory_resolve import resolve_inventory

            inv = resolve_inventory(pack, world_slug or None)
            catalog = getattr(inv, "item_catalog", None) if inv is not None else None
            if catalog:
                cat = next((c for c in catalog if c.id == item_id), None)
                if cat is not None and cat.damage is not None:
                    return cat.damage
        return None

    # Priority 3: an unarmed strike falls to the genre unarmed-damage floor.
    if wanted in _UNARMED_TERMS:
        rules = getattr(pack, "rules", None)
        return getattr(rules, "unarmed_damage", None) if rules is not None else None

    # Named weapon not carried and not an unarmed strike → loud (caller).
    return None


# ===========================================================================
# wn_attack
# ===========================================================================


class WnAttackArgs(BaseModel):
    attacker: str = Field(..., min_length=1, description="Name of the attacking PC/NPC.")
    target: str = Field(..., min_length=1, description="Name of the target being attacked.")
    weapon: str = Field(
        ...,
        min_length=1,
        description="The weapon (or 'fists' for an unarmed strike) the attacker uses.",
    )


@tool(
    name="wn_attack",
    description=(
        "Resolve a Without Number attack: the engine rolls d20 + the attacker's "
        "attribute modifier vs the target's Armor Class. On a hit the equipped "
        "weapon's damage dice resolve and land on the target's HP pool; the "
        "narrator describes FROM the engine's result, it does not invent hit or "
        "damage. A hit with no resolvable weapon damage is a loud error, never a "
        "silent 0-damage hit. WN-family only (swn/wwn/cwn/awn)."
    ),
    category=ToolCategory.WRITE,
    ruleset=WN_FAMILY_SLUGS,
)
async def wn_attack(args: WnAttackArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    module, cfg, slug = _require_wn(ctx)

    snapshot = session.snapshot
    atk = _resolve_actor(snapshot, args.attacker)
    if atk is None:
        return ToolResult.not_found(f"unknown actor: {args.attacker!r}")
    attacker_core, attacker_stats, _atk_level = atk
    target_core = snapshot.find_creature_core(args.target)
    if target_core is None:
        return ToolResult.not_found(f"unknown target: {args.target!r}")

    # To-hit: d20 + the better of the attacker's STR/DEX modifier vs target AC.
    # The narrator tool carries no beat, so attack_bonus/combat_skill are 0 —
    # class-progression to-hit isn't on the snapshot; the modifier is the
    # attribute mod the bound module computes (honest about what data we have).
    amap = cfg.attribute_map
    str_mod = module.stat_modifier(attacker_stats, amap["STRENGTH"])
    dex_mod = module.stat_modifier(attacker_stats, amap["DEXTERITY"])
    to_hit_attr = amap["STRENGTH"] if str_mod >= dex_mod else amap["DEXTERITY"]
    d20 = random.randint(1, 20)
    target_ac = int(target_core.armor_class)
    outcome = module.resolve_opponent_attack(
        attacker_stats=attacker_stats,
        stat_check=to_hit_attr,
        attack_bonus=0,
        combat_skill=0,
        target_ac=target_ac,
        d20=d20,
    )

    damage = 0
    if outcome.hit:
        spec = _resolve_weapon_damage(
            actor_core=attacker_core,
            weapon_name=args.weapon,
            pack=ctx.genre_pack,
            world_slug=ctx.world_id,
        )
        if spec is None:
            return ToolResult.error(
                f"{args.attacker!r} hit {args.target!r} with {args.weapon!r} but no "
                "resolvable weapon damage spec (the named weapon is not carried, or "
                "carries no damage, and there is no unarmed floor) — refusing a "
                "silent 0-damage hit (CLAUDE.md No Silent Fallbacks)",
                recoverable=True,
            )
        damage = spec.roll(_RNG)
        target_core.apply_hp_delta(-damage)
        ctx.repository.save(snapshot)

    wn_attack_resolved_span(
        slug=slug,
        actor=args.attacker,
        target=args.target,
        weapon=args.weapon,
        hit=outcome.hit,
        d20=d20,
        modifier=outcome.modifier,
        attack_total=outcome.attack_total,
        target_ac=target_ac,
        damage=damage,
    )
    ctx.otel_span.set_attribute("tool.wn_attack.actor", args.attacker)
    ctx.otel_span.set_attribute("tool.wn_attack.target", args.target)
    ctx.otel_span.set_attribute("tool.wn_attack.hit", outcome.hit)
    ctx.otel_span.set_attribute("tool.wn_attack.damage", damage)

    return ToolResult.ok(
        {
            "attacker": args.attacker,
            "target": args.target,
            "weapon": args.weapon,
            "hit": outcome.hit,
            "d20": d20,
            "modifier": outcome.modifier,
            "attack_total": outcome.attack_total,
            "target_ac": target_ac,
            "damage": damage,
        }
    )


# ===========================================================================
# wn_skill_check
# ===========================================================================


class WnSkillCheckArgs(BaseModel):
    actor: str = Field(..., min_length=1, description="Name of the PC/NPC making the check.")
    skill: str = Field(..., min_length=1, description="The skill being used (e.g. 'Sneak').")
    attribute: str = Field(
        ..., min_length=1, description="The attribute keying the check (e.g. 'DEXTERITY')."
    )
    skill_level: int = Field(
        default=0,
        ge=-1,
        le=4,
        description=(
            "Sheet-derived skill level (-1 unskilled .. +4 mastery). Narrator-supplied "
            "(the engine stores no skill sheet) — mirrors the dice-path CheckThrowPayload."
        ),
    )
    difficulty: str = Field(
        ...,
        min_length=1,
        description="Difficulty ladder KEY (easy/routine/tricky/hard/formidable) — never a literal DC.",
    )


@tool(
    name="wn_skill_check",
    description=(
        "Resolve a Without Number 2d6 skill check: the engine rolls 2d6 + the "
        "attribute modifier + skill_level vs the pack's authored difficulty ladder "
        "(the server is the only DC author — pass a ladder KEY like 'hard', never a "
        "number). Returns success and margin so the narrator describes the outcome "
        "FROM the roll. An unknown ladder key is a loud error. WN-family only."
    ),
    category=ToolCategory.READ,
    ruleset=WN_FAMILY_SLUGS,
)
async def wn_skill_check(args: WnSkillCheckArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    module, cfg, slug = _require_wn(ctx)

    snapshot = session.snapshot
    actor = _resolve_actor(snapshot, args.actor)
    if actor is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")
    actor_core, stats, _level = actor

    if args.difficulty not in cfg.difficulties:
        raise ValueError(
            f"unknown difficulty key {args.difficulty!r}; ladder has {sorted(cfg.difficulties)}"
        )

    params = module.check_params(
        stats=stats,
        attribute=args.attribute,
        skill_level=args.skill_level,
        difficulty_key=args.difficulty,
        label=f"{args.skill} check",
        cfg=cfg,
        character_core=actor_core,
    )
    rolls = [random.randint(1, params.sides) for _ in range(params.count)]
    total = sum(rolls) + params.modifier
    success = total >= params.difficulty
    margin = total - params.difficulty

    wn_skill_check_resolved_span(
        slug=slug,
        actor=args.actor,
        skill=args.skill,
        attribute=args.attribute,
        difficulty=params.difficulty,
        total=total,
        modifier=params.modifier,
        success=success,
    )
    ctx.otel_span.set_attribute("tool.wn_skill_check.actor", args.actor)
    ctx.otel_span.set_attribute("tool.wn_skill_check.success", success)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "skill": args.skill,
            "attribute": args.attribute,
            "rolls": rolls,
            "modifier": params.modifier,
            "total": total,
            "difficulty": params.difficulty,
            "success": success,
            "margin": margin,
        }
    )


# ===========================================================================
# wn_save
# ===========================================================================


class WnSaveArgs(BaseModel):
    actor: str = Field(..., min_length=1, description="Name of the PC/NPC making the save.")
    save: str = Field(
        ...,
        min_length=1,
        description=(
            "Save category — module-owned vocabulary (physical/evasion/mental on swn; "
            "luck additionally on wwn/cwn/awn). An unknown category fails loud."
        ),
    )
    effect: str = Field(
        default="",
        description="What the actor is saving against — span + narration evidence.",
    )


@tool(
    name="wn_save",
    description=(
        "Resolve a Without Number saving throw: the engine rolls d20 + the best "
        "applicable attribute modifier vs the derived target (save_base - (level-1)). "
        "The save CATEGORY vocabulary is owned by the bound module — 'luck' exists on "
        "wwn/cwn/awn but not swn — so one contract serves the family without forking. "
        "An unknown category is a loud error. WN-family only."
    ),
    category=ToolCategory.READ,
    ruleset=WN_FAMILY_SLUGS,
)
async def wn_save(args: WnSaveArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    module, cfg, slug = _require_wn(ctx)

    snapshot = session.snapshot
    actor = _resolve_actor(snapshot, args.actor)
    if actor is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")
    actor_core, stats, level = actor

    # save_params raises ValueError naming an unknown category — let it propagate
    # as a loud dispatch error (No Silent Fallbacks).
    params = module.save_params(
        stats=stats,
        save=args.save,
        level=level,
        label=f"{args.save} save",
        cfg=cfg,
        character_core=actor_core,
    )
    d20 = random.randint(1, params.sides)
    success = (d20 + params.modifier) >= params.difficulty

    wn_save_resolved_span(
        slug=slug,
        actor=args.actor,
        save=args.save,
        effect=args.effect,
        target=params.difficulty,
        d20=d20,
        modifier=params.modifier,
        success=success,
    )
    ctx.otel_span.set_attribute("tool.wn_save.actor", args.actor)
    ctx.otel_span.set_attribute("tool.wn_save.save", args.save)
    ctx.otel_span.set_attribute("tool.wn_save.success", success)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "save": args.save,
            "effect": args.effect,
            "d20": d20,
            "modifier": params.modifier,
            "target": params.difficulty,
            "success": success,
        }
    )


# ===========================================================================
# wn_adjudicate_dead_premise
# ===========================================================================


class WnDeadPremiseArgs(BaseModel):
    actor: str = Field(..., min_length=1, description="Name of the acting PC/NPC.")
    action: str = Field(..., min_length=1, description="The action whose premise just went dead.")
    gone_target: str = Field(
        ..., min_length=1, description="The target that is no longer present/valid."
    )
    ruling: Literal["redirect", "fizzle"] = Field(
        ...,
        description=(
            "'redirect': the action finds a new valid landing (routed as a SEPARATE "
            "wn_attack/etc. call). 'fizzle': the action comes to nothing. Closed enum — "
            "the narrator cannot invent a third outcome."
        ),
    )
    reason: str = Field(
        default="",
        description="Why this ruling — recorded on the span (the §8 OTEL 'why' column).",
    )


@tool(
    name="wn_adjudicate_dead_premise",
    description=(
        "Adjudicate a Without Number dead premise — an action whose target/premise "
        "vanished before it resolved (the husk dropped before the blade landed). The "
        "narrator picks 'redirect' (the action finds a new landing, routed as a "
        "SEPARATE tool call) or 'fizzle' (it comes to nothing). This tool mutates "
        "NOTHING mechanical — it records the ruling and reason on the GM-panel span. "
        "WN-family only."
    ),
    category=ToolCategory.READ,
    ruleset=WN_FAMILY_SLUGS,
)
async def wn_adjudicate_dead_premise(args: WnDeadPremiseArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    _module, _cfg, slug = _require_wn(ctx)

    snapshot = session.snapshot
    actor = _resolve_actor(snapshot, args.actor)
    if actor is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")

    # §8: nothing mechanical until the narrator routes a follow-up — record only.
    wn_dead_premise_adjudicated_span(
        slug=slug,
        actor=args.actor,
        action=args.action,
        gone_target=args.gone_target,
        ruling=args.ruling,
        reason=args.reason,
    )
    ctx.otel_span.set_attribute("tool.wn_dead_premise.actor", args.actor)
    ctx.otel_span.set_attribute("tool.wn_dead_premise.ruling", args.ruling)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "action": args.action,
            "gone_target": args.gone_target,
            "ruling": args.ruling,
            "reason": args.reason,
        }
    )
