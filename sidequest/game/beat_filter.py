"""Per-class beat filter — single source of truth for 'what can the player do this turn?'.

Per spec docs/superpowers/specs/2026-05-08-cnc-bx-class-beats-morale-design.md §4.3
and 2026-05-06 magic-system spec §3.5 (memorization wiring, story 47-10).

The filter resolves three independent gates:

  1. class_filter on each beat (None = universal; non-empty = whitelist)
  2. class_def.encounter_beat_choices intersection (per-class whitelist)
  3. cast_spell resource gates:
     a. slot gate — spell_slots_remaining >= 1.0
     b. prepared-list gate — actor has at least one spell prepared at any
        level (story 47-10 dual-plugin pivot)

The slot and prepared-list gates fail independently, with distinct OTEL
reasons so the GM panel can tell "Mage out of slots" from "Mage didn't
memorize anything this morning". The two gates are NOT collapsed into one.
"""

from __future__ import annotations

import re
from typing import Any

from sidequest.game.beat_kinds import BeatKind
from sidequest.game.wwn_magic import SpellcastingState
from sidequest.genre.error import PackError
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, DamageChannel

# Story 106-4 Part C — transient inventory item-use beats. The confrontation
# beat menu scans the actor's carried inventory and offers a "Drink <potion>"
# beat for each usable heal consumable so a player can drink mid-fight (the
# Zork-Problem / Agency fix: the verb set was closed at the most consequential
# moment). The beat id encodes the item slug so the dispatch can match it back
# to the inventory stack to consume. Resolution is auto-success (no roll) and
# costs the Main Action — the opponent still acts on its initiative slot
# (Keith, 2026-06-14; WWN-faithful, consistent with 106-2 Option A).
ITEM_USE_BEAT_PREFIX = "use_item:"


def item_slug(name: str) -> str:
    """Stable, reversible-enough slug for an item name (``"Potion of Mending"``
    → ``"potion_of_mending"``). The dispatch matches a committed item-use beat
    back to an inventory item by comparing this slug, so it must be a pure
    function of the name with no external state."""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def item_use_beat_id(name: str) -> str:
    """The transient beat id for using the named item."""
    return f"{ITEM_USE_BEAT_PREFIX}{item_slug(name)}"


def is_item_use_beat(beat_id: str) -> bool:
    """True iff ``beat_id`` is a transient inventory item-use beat."""
    return beat_id.startswith(ITEM_USE_BEAT_PREFIX)


def _is_consumable(item: dict[str, Any]) -> bool:
    """Mirror of narration_apply._is_consumable_item (kept local to avoid a
    game→server layering import): a genuine single-use item — category
    ``consumable`` OR a ``consumable`` tag. Only these may be spent on use."""
    category = str(item.get("category", "") or "").strip().lower()
    if category == "consumable":
        return True
    tags = item.get("tags") or []
    if isinstance(tags, (list, tuple, set)):
        return any(str(tag).strip().lower() == "consumable" for tag in tags)
    return False


def item_use_beats(inventory_items: list[dict[str, Any]] | None) -> list[BeatDef]:
    """Transient item-use BeatDefs for the usable heal consumables carried.

    A usable item is a genuine consumable (``_is_consumable``) carrying a
    truthy ``heal_amount`` — the effect the dispatch will roll and apply. Two
    identical stacks collapse to one beat (the action is the same; using it
    consumes one). ``kind``/``stat_check`` are inert for these beats: the
    dispatch intercepts the ``use_item:`` id BEFORE confrontation resolution,
    so they never feed the dial/attack engine — they exist only to satisfy the
    BeatDef model and render the tile.
    """
    if not inventory_items:
        return []
    beats: list[BeatDef] = []
    seen: set[str] = set()
    for item in inventory_items:
        name = str(item.get("name", "") or "").strip()
        if not name or not _is_consumable(item) or not item.get("heal_amount"):
            continue
        slug = item_slug(name)
        if slug in seen:
            continue
        seen.add(slug)
        beats.append(
            BeatDef(
                id=item_use_beat_id(name),
                label=f"Drink {name}",
                kind=BeatKind.push,
                base=0,
                stat_check="CON",
                flavor="Spend your action to use a carried item.",
            )
        )
    return beats


# Story 108-8 (epic 108, ADR-143) — the Without-Number action set. Under a WN
# binding the WN engine OWNS the round (SOUL "Bind the Ruleset, Don't Balance It"):
# 108-3 strips the native combat beats off every WWN ``hp_depletion`` def, leaving
# ``cdef.beats == []``, so the runtime must SYNTHESIZE a transient beat for each
# core WN action — independent of cdef.beats — exactly as ``item_use_beats`` above
# synthesizes the "Drink <potion>" beat that is not authored on the cdef. The
# dispatch intercepts these ids BEFORE the cdef beat lookup, gated on
# ``isinstance(ruleset, WithoutNumberRulesetModule)`` (dice.py + wn_round.py).
#
# item-use (``use_item:<slug>``) and cast (``cast_spell``) already have their own
# dispatch routes and are NOT in this set. The WN disengage/defensive actions
# (``run`` / ``fighting_withdrawal`` / ``total_defense``) ARE in this set as of
# story 152-1 — see the 152-1 block below for their (non-strike) synthesis.
#
# Gate caveat: a NATIVE pack may itself author a beat literally named ``attack``
# (tests/fixtures/packs/test_genre). The isinstance gate at the call site leaves
# native ids on the authored-beat lookup, so this only fires under a WN binding.
WN_ATTACK_BEAT_ID = "attack"

# Story 152-1 (ADR-143, WWN SRD §2.4.4) — the WWN defensive / move actions. WWN
# defense is Armor Class, NOT the native brace/break_contact reprisal-mitigation
# model (which is removed from the WN path — SOUL "Bind the Ruleset, Don't Balance
# It"). Total Defense is an Instant Action (+2 Melee/Ranged AC + Shock immunity);
# Fighting Withdrawal is a safe Main-Action disengage; Run is the plain flee that
# provokes a free attack. All three resolve NO offensive strike on the actor's own
# slot — their effect is read at the opponent's slot (Total Defense → AC posture,
# ``dice._defensive_posture_for_reprisal``) or at the actor's own slot as a
# disengage (``wn_round`` flee intercept).
WN_TOTAL_DEFENSE_BEAT_ID = "total_defense"
WN_FIGHTING_WITHDRAWAL_BEAT_ID = "fighting_withdrawal"
WN_RUN_BEAT_ID = "run"
_WN_ACTION_BEAT_IDS = frozenset(
    {
        WN_ATTACK_BEAT_ID,
        WN_TOTAL_DEFENSE_BEAT_ID,
        WN_FIGHTING_WITHDRAWAL_BEAT_ID,
        WN_RUN_BEAT_ID,
    }
)
# Story 152-2 (ADR-143, WWN SRD §4.2) — the synthesized WWN cast action. 108-3
# stripped ``cast_spell`` from every WWN hp_depletion combat def (cdef.beats == []);
# the WWN engine OWNS the action set, so cast is a synthesized transient beat, not an
# authored cdef entry. It is DELIBERATELY NOT in ``_WN_ACTION_BEAT_IDS``: cast routing
# is WWN-ruleset-specific (the cast spine — ``_resolve_wwn_cast_for_beat`` — has no
# non-WWN arm), so each call site gates synthesis on the ``wwn`` binding and a
# cast_spell commit on any OTHER ruleset stays a loud unknown-beat raise (No Silent
# Fallbacks), rather than synthesizing-but-silently-not-resolving.
WN_CAST_SPELL_BEAT_ID = "cast_spell"
# The disengage (move) actions that withdraw the actor from melee: ``run`` provokes
# one free opportunity attack from each adjacent opponent; ``fighting_withdrawal``
# does not (SRD §2.4.4).
_WN_FLEE_ACTION_IDS = frozenset({WN_FIGHTING_WITHDRAWAL_BEAT_ID, WN_RUN_BEAT_ID})
# Actions that resolve no offensive strike on the actor's own slot — handled by the
# wn_round flee/posture intercept BEFORE the strike-resolution path.
_WN_NONOFFENSIVE_ACTION_IDS = frozenset(
    {WN_TOTAL_DEFENSE_BEAT_ID, WN_FIGHTING_WITHDRAWAL_BEAT_ID, WN_RUN_BEAT_ID}
)


def is_wn_action_beat(beat_id: str) -> bool:
    """True iff ``beat_id`` is a synthesized Without-Number action beat (story 108-8 /
    152-1: attack + the defensive/move actions).

    A pure id check — the WN binding gate lives at the dispatch call site, mirroring
    ``is_item_use_beat``. Native packs route the same id through the cdef lookup."""
    return beat_id in _WN_ACTION_BEAT_IDS


def is_wn_flee_action(beat_id: str) -> bool:
    """True iff ``beat_id`` is a WWN disengage/move action (``run`` /
    ``fighting_withdrawal``) — story 152-1, WWN SRD §2.4.4."""
    return beat_id in _WN_FLEE_ACTION_IDS


def is_wn_nonoffensive_action(beat_id: str) -> bool:
    """True iff ``beat_id`` is a WWN action that resolves no offensive strike on the
    actor's own slot (Total Defense / Fighting Withdrawal / Run) — story 152-1."""
    return beat_id in _WN_NONOFFENSIVE_ACTION_IDS


def wn_action_beat(beat_id: str) -> BeatDef:
    """The transient ``BeatDef`` for a WN action id (story 108-8 / 152-1).

    ``attack`` → a plain STR strike carrying no authored damage: the weapon dice
    resolve from the actor's inventory (``damage_roll`` priority 2/3) or the genre
    unarmed floor — the same source the now-stripped native combat beat drew from.
    ``damage_channel`` is ``strike`` so ``dice._resolve_wn_committed_action`` lands
    the weapon dice on the target's ablative HP (ADR-114) with the native scaffolding
    cut (ADR-143). ``attack_bonus``/``combat_skill`` default to 0 — a synthesized
    action carries no class to-hit progression, matching the ``wn_attack`` tool.

    The defensive / move actions (Total Defense / Fighting Withdrawal / Run) carry
    NO ``strike`` channel — they deal no offensive damage on the actor's own slot
    (their effect is the AC posture or the disengage). A DEX ``stat_check`` keeps the
    pre-seal throw well-formed; the throw outcome does not gate the action's effect.
    """
    if beat_id not in _WN_ACTION_BEAT_IDS:
        raise PackError(f"{beat_id!r} is not a synthesizable WN action beat")
    if beat_id == WN_ATTACK_BEAT_ID:
        return BeatDef(
            id=beat_id,
            label="Attack",
            kind=BeatKind.strike,
            base=0,
            stat_check="STR",
            damage_channel=DamageChannel.strike,
        )
    _labels = {
        WN_TOTAL_DEFENSE_BEAT_ID: "Total Defense",
        WN_FIGHTING_WITHDRAWAL_BEAT_ID: "Fighting Withdrawal",
        WN_RUN_BEAT_ID: "Run",
    }
    # ``push`` ("pursue a discrete narrative goal — flee, disengage, hold") carries
    # no ``target_tag`` requirement and no ``strike`` damage channel. The kind is
    # inert on the WWN path: run/fighting_withdrawal are intercepted before the
    # strike resolver and total_defense's effect is the AC posture, not the kind.
    return BeatDef(
        id=beat_id,
        label=_labels[beat_id],
        kind=BeatKind.push,
        base=0,
        stat_check="DEX",
    )


def wn_cast_beat() -> BeatDef:
    """The transient ``BeatDef`` for the synthesized WWN cast action (story 152-2).

    108-3 stripped ``cast_spell`` from every WWN hp_depletion combat def; the WWN
    engine OWNS the action set (ADR-143), so cast is synthesized, not looked up in
    ``cdef.beats``. This beat is an inert VEHICLE — the real resolution is the WWN
    cast spine (``_resolve_wwn_cast_for_beat``), gated downstream on
    ``beat.id == "cast_spell"``:

    * NO ``strike`` damage channel — spell damage flows through the cast spine, not
      the weapon channel (``damage_channel`` defaults to ``none``).
    * ``push`` kind — the pre-cast d20 outcome does NOT gate the cast: WWN High Magic
      casting is automatic and the DEFENDER saves (SRD §4.2), so the spine runs
      regardless of the throw tier.
    * ``INT`` ``stat_check`` keeps the pre-cast throw well-formed.

    Unlike ``attack``/the defensive actions this is NOT a member of
    ``_WN_ACTION_BEAT_IDS`` / ``is_wn_action_beat`` — cast routing is WWN-specific, so
    each dispatch seam gates synthesis on the ``wwn`` binding (a non-WWN ``cast_spell``
    stays a loud unknown-beat raise).
    """
    return BeatDef(
        id=WN_CAST_SPELL_BEAT_ID,
        label="Cast Spell",
        kind=BeatKind.push,
        base=0,
        stat_check="INT",
    )


def _has_any_prepared(prepared_spells: dict[int, list[str]] | None) -> bool:
    """True iff the actor has at least one spell prepared at any level."""
    if not prepared_spells:
        return False
    return any(spells for spells in prepared_spells.values())


def beats_available_for(
    confrontation: ConfrontationDef,
    class_def: ClassDef,
    spell_slots_remaining: float,
    prepared_spells: dict[int, list[str]] | None = None,
    spellcasting: SpellcastingState | None = None,
    inventory_items: list[dict[str, Any]] | None = None,
) -> list[BeatDef]:
    """Return the BeatDefs the given class can select this turn.

    ``prepared_spells`` (story 47-10 addition) is optional for backward
    compatibility — when omitted (or None), the prepared-list gate is
    skipped and behavior matches the pre-47-10 contract. Existing
    callers (narrator.py, orchestrator.py) continue to work; new
    callers should pass it.

    ``spellcasting`` (WWN arm, Task 5): when a ``SpellcastingState`` is
    provided, the cast_spell gate uses WWN economy (casts_remaining +
    non-empty prepared list) and completely ignores ``spell_slots_remaining``
    / ``prepared_spells``.  When ``spellcasting is None`` the existing B/X
    behavior is preserved byte-for-byte.
    """
    if not class_def.encounter_beat_choices:
        raise PackError(f"class {class_def.display_name!r} has empty encounter_beat_choices")

    pool: list[BeatDef] = []
    for beat in confrontation.beats:
        # Gate 1 — class_filter whitelist. A non-empty class_filter restricts
        # the beat to the listed classes; class restriction is enforced HERE.
        if beat.class_filter is not None and class_def.display_name not in beat.class_filter:
            continue
        # Universal beats (class_filter is None) are available to every class
        # without per-class ``encounter_beat_choices`` enumeration — this is
        # the documented "class_filter None = universal" semantics. The
        # ``encounter_beat_choices`` whitelist (gate 2 below) curates only
        # class-specific beats; applying it to universals filtered out every
        # non-combat confrontation's beats (chase/negotiation/standoff are all
        # universal and no class enumerates them), starving the UI of choices.
        if beat.class_filter is None:
            pool.append(beat)
            continue
        # WWN cast_spell (89-5): gate 1 (class_filter) is the ONLY class
        # gate that offers it on the WWN arm. The heavy_metal chassis
        # contract deliberately forbids cast_spell in every class's
        # encounter_beat_choices ("the rules.yaml class_filter is the only
        # gate that should offer it"), so running gate 2 here starved every
        # WWN caster of the beat. The WWN economy still gates casts/prepared.
        if beat.id == "cast_spell" and spellcasting is not None:
            if spellcasting.casts_remaining < 1:
                continue
            if not spellcasting.prepared:
                continue
            pool.append(beat)
            continue
        # Gate 2 — per-class whitelist for class-specific beats.
        if beat.id not in class_def.encounter_beat_choices:
            continue
        if beat.id == "cast_spell":
            # B/X arm — unchanged (the WWN arm exited above).
            if spell_slots_remaining < 1.0:
                continue
            # Prepared-list gate runs only when the caller opts in by
            # passing prepared_spells. Backward-compat: existing callers
            # that don't pass the param skip this gate and rely on the
            # slot gate alone.
            if prepared_spells is not None and not _has_any_prepared(prepared_spells):
                continue
        pool.append(beat)
    # WWN cast synthesis (story 152-2 / 89-5, ADR-143): 108-3 stripped cast_spell
    # from WWN combat cdefs (cdef.beats == []), so the loop above can never
    # surface it — the WWN engine OWNS the action set, so cast is a synthesized
    # transient beat, not authored content. The resolution path (wn_round.py /
    # dice.py) already synthesizes the same beat on commit; this is its
    # selection-menu twin (without it, a WWN caster can resolve a forced cast but
    # never SELECT one). Offer it to a WWN caster — a class carrying a wwn_magic
    # block — whose WWN economy (a remaining cast + a prepared spell) permits a
    # cast this turn, in hp_depletion combat. A Warrior (wwn_magic is None) never
    # sees it even if a spellcasting state is wrongly supplied (gate must not
    # loosen). ``spellcasting is not None`` is the WWN-arm signal.
    if (
        spellcasting is not None
        and class_def.wwn_magic is not None
        and confrontation.win_condition == "hp_depletion"
        and spellcasting.casts_remaining >= 1
        and spellcasting.prepared
        and not any(b.id == WN_CAST_SPELL_BEAT_ID for b in pool)
    ):
        pool.append(wn_cast_beat())
    # Story 106-4 Part C: append transient item-use beats from the actor's
    # inventory, gated to hp_depletion combat — a heal consumable is only
    # usable where HP is the track (a chase/social cdef has no HP to restore,
    # so offering "Drink Potion" there is a dead affordance). Appended AFTER
    # the authored pool so item beats sort to the end of the menu.
    if inventory_items and confrontation.win_condition == "hp_depletion":
        pool.extend(item_use_beats(inventory_items))
    return pool


def cast_spell_rejection_reason(
    confrontation: ConfrontationDef,
    class_def: ClassDef,
    spell_slots_remaining: float,
    prepared_spells: dict[int, list[str]] | None = None,
    spellcasting: SpellcastingState | None = None,
) -> str | None:
    """Why was cast_spell filtered out for this actor?

    Returns one of:
      - ``None`` — cast_spell was selectable (no rejection), OR ``prepared_spells``
        was omitted (backward-compat caller — gate is dormant)
      - ``"no_slots"`` — slot bar at zero; rest required (B/X) OR
        casts_remaining == 0 (WWN — semantically "needs rest")
      - ``"unprepared"`` — caller passed a non-None ``prepared_spells`` and the
        actor has no spells prepared at any level (B/X), OR WWN
        ``spellcasting.prepared`` is empty
      - ``"class"`` — class isn't allowed cast_spell at all (Fighter/Thief)
      - ``"absent"`` — beat isn't in this confrontation's pool

    When ``spellcasting`` is provided the WWN economy takes precedence and
    ``spell_slots_remaining``/``prepared_spells`` are ignored.

    Used by OTEL emitters to stamp distinct decision values on the
    confrontation.beat_filter span — the GM panel reads them to tell
    "out of slots" from "didn't prep" without parsing prose.
    """
    cast_beat = next((b for b in confrontation.beats if b.id == "cast_spell"), None)
    if cast_beat is None:
        return "absent"
    if cast_beat.class_filter is not None and class_def.display_name not in cast_beat.class_filter:
        return "class"
    if spellcasting is not None:
        # WWN arm (89-5): class_filter is the only class gate — symmetric
        # with beats_available_for; classes never list cast_spell in
        # encounter_beat_choices on the WWN chassis. Gate on
        # SpellcastingState, ignore B/X slots.
        if spellcasting.casts_remaining < 1:
            return "no_slots"
        if not spellcasting.prepared:
            return "unprepared"
        return None
    if "cast_spell" not in (class_def.encounter_beat_choices or []):
        return "class"
    # B/X arm — unchanged.
    if spell_slots_remaining < 1.0:
        return "no_slots"
    # Symmetric with beats_available_for: when prepared_spells is omitted
    # (None — backward-compat callers), the prepared-list gate is skipped
    # and cast_spell is considered selectable. Only callers that explicitly
    # pass an empty/non-empty dict trigger the unprepared check.
    if prepared_spells is not None and not _has_any_prepared(prepared_spells):
        return "unprepared"
    return None
