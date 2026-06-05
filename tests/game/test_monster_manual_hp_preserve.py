"""BUG 2b (eh-opp-damage) — per-turn Monster-Manual injection must NOT clobber
the HP of an actor already seated in the active encounter.

PLAYTEST (elemental_harmony/burning_peace, WWN): ``monster_manual.injected
... in_combat=True patches=11`` fires on EVERY combat turn, re-materializing the
seated opponent. The merge leg (``GameSnapshot._merge_npc_patch``)
unconditionally reset ``npc.core.hp`` to a FULL pool from the patch's ``hp`` claim
every turn, so an opponent the player had damaged (0/8) healed back (→ 6/8) on the
next turn — combat could be neither won nor lost (an infinite stalemate, paired
with BUG 2a).

The fix: when an NpcPatch re-asserts a creature's ``hp`` for an NPC that already
exists, only (re)seed the pool MAX/base_max — never overwrite the live
``current`` of a creature already taking damage. A genuinely NEW creature
(``_npc_from_patch``) still gets a full pool. The decision emits a
``monster_manual.hp_preserved`` OTEL span so the GM panel can confirm the
re-injection kept the damaged HP rather than silently healing the enemy.

These tests pin the merge-seam behavior directly (synthetic snapshot + patch),
plus the OTEL lie-detector span, per the repo's "no source-text wiring tests"
rule.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, NpcPatch, WorldStatePatch


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        characters=[],
    )


def _creature_npc(name: str, *, current: int, maximum: int) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=current, max=maximum, base_max=maximum),
        ),
        creature_id="rider",
        threat_level=2,
        manual_origin=True,
    )


# ---------------------------------------------------------------------------
# Core invariant: a re-injection patch must NOT heal a damaged creature
# ---------------------------------------------------------------------------


def test_reinjection_preserves_damaged_current_hp() -> None:
    """A creature damaged to 2/8 must STAY at 2/8 when the per-turn Monster
    Manual re-injects the same creature with a fresh ``hp=8`` claim. RED before
    the fix: ``_merge_npc_patch`` reset current to 8."""
    snap = _snapshot()
    snap.npcs.append(_creature_npc("Approaching Riders", current=2, maximum=8))

    # The per-turn injection re-emits the creature with its content HP claim.
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(
                    name="Approaching Riders",
                    creature_id="rider",
                    threat_level=2,
                    hp=8,
                    manual_origin=True,
                )
            ]
        )
    )

    npc = next(n for n in snap.npcs if n.core.name == "Approaching Riders")
    assert npc.core.hp.current == 2, (
        f"re-injection must PRESERVE the damaged current HP (2/8); "
        f"got {npc.core.hp.current}/{npc.core.hp.max} — the opponent healed"
    )
    # The max/base_max may be (re)seeded from the content claim — that is fine.
    assert npc.core.hp.max == 8


def test_reinjection_preserves_zero_hp_so_win_condition_can_fire() -> None:
    """A creature dropped to 0/8 must STAY at 0 across a re-injection — otherwise
    the per-turn inject resurrects a defeated opponent and the hp_depletion win
    condition can never hold (the exact stalemate paired with BUG 2a)."""
    snap = _snapshot()
    snap.npcs.append(_creature_npc("Approaching Riders", current=0, maximum=8))

    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(name="Approaching Riders", creature_id="rider", hp=8, manual_origin=True)
            ]
        )
    )

    npc = next(n for n in snap.npcs if n.core.name == "Approaching Riders")
    assert npc.core.hp.current == 0, (
        f"a defeated (0 HP) opponent must NOT be resurrected by re-injection; "
        f"got {npc.core.hp.current}"
    )


def test_new_creature_still_gets_full_pool_from_patch() -> None:
    """A genuinely NEW creature (no existing Npc of that name) still materializes
    with a FULL pool from the patch's hp claim — the fix only guards the merge
    leg, never the fresh-spawn leg."""
    snap = _snapshot()
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(name="Fresh Rider", creature_id="rider", hp=8, manual_origin=True)
            ]
        )
    )
    npc = next(n for n in snap.npcs if n.core.name == "Fresh Rider")
    assert npc.core.hp.current == 8 and npc.core.hp.max == 8


def test_reinjection_emits_hp_preserved_otel_span(otel_capture) -> None:
    """The merge seam must emit ``monster_manual.hp_preserved`` when it keeps a
    damaged creature's current HP — the GM-panel lie-detector that the
    re-injection did NOT silently heal the enemy."""
    snap = _snapshot()
    snap.npcs.append(_creature_npc("Approaching Riders", current=3, maximum=8))

    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(name="Approaching Riders", creature_id="rider", hp=8, manual_origin=True)
            ]
        )
    )

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "monster_manual.hp_preserved"
    ]
    assert spans, (
        "an hp-preserving re-injection must emit monster_manual.hp_preserved; "
        f"got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs.get("npc_name") == "Approaching Riders"
    assert attrs.get("preserved_current") == 3
    assert attrs.get("patch_hp") == 8
