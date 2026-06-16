"""Game-state chassis registry — chassis as first-class entities, not inventory.

Per docs/design/rig-taxonomy.md locked decision α (sibling framework) and
the slice spec (docs/superpowers/specs/2026-04-29-rig-mvp-coyote-star-design.md
§2.1) chassis state lives in its own container. Wave 2A (story 45-47)
removed the projection into the now-dropped ``npc_registry`` — chassis
reach the narrator prompt via the dedicated chassis-voice section
(``register_chassis_voice_section``), not the NPC roster.

Slice scope: ChassisInstance + bond ledger + lineage + bond mutation +
tier derivation. Hardpoints, subsystems, damage_history, registration
are deferred fields and not authored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sidequest.genre.models.chassis import BondTier, ChassisVoiceSpec
from sidequest.genre.models.rigs_world import OceanScores

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot

_TIER_THRESHOLDS: list[tuple[float, BondTier]] = [
    (-0.85, "severed"),
    (-0.45, "hostile"),
    (-0.10, "strained"),
    (0.10, "neutral"),
    (0.40, "familiar"),
    (0.80, "trusted"),
    (1.01, "fused"),
]


def derive_bond_tier(strength: float) -> BondTier:
    """Map a bond_strength scalar in [-1.0, 1.0] to a discrete tier."""
    for ceiling, tier in _TIER_THRESHOLDS:
        if strength < ceiling:
            return tier
    return "fused"


class BondHistoryEvent(BaseModel):
    model_config = {"extra": "forbid"}
    turn_id: int
    delta_character: float
    delta_chassis: float
    reason: str
    confrontation_id: str | None = None


class BondLedgerEntry(BaseModel):
    model_config = {"extra": "forbid"}
    character_id: str
    bond_strength_character_to_chassis: float = Field(default=0.0, ge=-1.0, le=1.0)
    bond_strength_chassis_to_character: float = Field(default=0.0, ge=-1.0, le=1.0)
    bond_tier_character: BondTier = "neutral"
    bond_tier_chassis: BondTier = "neutral"
    history: list[BondHistoryEvent] = Field(default_factory=list)


class ChassisLineageEntry(BaseModel):
    model_config = {"extra": "forbid"}
    turn_id: int
    kind: str
    narrative_seed: str
    confrontation_id: str | None = None


class ChassisInstance(BaseModel):
    """Live chassis state. Source of truth — projected to the narrator via
    the chassis-voice section, not via the (dropped) ``npc_registry``."""

    model_config = {"extra": "forbid"}

    id: str
    name: str
    class_id: str
    OCEAN: OceanScores = Field(default_factory=OceanScores)
    voice: ChassisVoiceSpec | None = None
    interior_rooms: list[str] = Field(default_factory=list)
    bond_ledger: list[BondLedgerEntry] = Field(default_factory=list)
    lineage: list[ChassisLineageEntry] = Field(default_factory=list)

    def bond_for(self, character_id: str) -> BondLedgerEntry | None:
        for entry in self.bond_ledger:
            if entry.character_id == character_id:
                return entry
        return None


@dataclass
class BondEventResult:
    tier_character_before: BondTier
    tier_character_after: BondTier
    tier_chassis_before: BondTier
    tier_chassis_after: BondTier
    tier_character_crossed: bool
    tier_chassis_crossed: bool


def apply_bond_event(
    *,
    chassis: ChassisInstance,
    character_id: str,
    delta_character: float,
    delta_chassis: float,
    reason: str,
    confrontation_id: str | None,
    turn_id: int,
) -> BondEventResult:
    """Mutate the bond ledger; return tier-crossing info for the caller.

    Caller is responsible for emitting the rig.bond_event span using the
    returned tier metadata. Span emission is intentionally NOT done in-line
    so unit tests don't pull in the OTEL exporter.
    """
    entry = chassis.bond_for(character_id)
    if entry is None:
        raise ValueError(
            f"chassis {chassis.id!r} has no bond ledger entry for "
            f"character {character_id!r} — was world-load bond_seed run?"
        )

    tier_char_before = entry.bond_tier_character
    tier_chassis_before = entry.bond_tier_chassis

    entry.bond_strength_character_to_chassis = max(
        -1.0,
        min(1.0, entry.bond_strength_character_to_chassis + delta_character),
    )
    entry.bond_strength_chassis_to_character = max(
        -1.0,
        min(1.0, entry.bond_strength_chassis_to_character + delta_chassis),
    )

    entry.bond_tier_character = derive_bond_tier(entry.bond_strength_character_to_chassis)
    entry.bond_tier_chassis = derive_bond_tier(entry.bond_strength_chassis_to_character)

    entry.history.append(
        BondHistoryEvent(
            turn_id=turn_id,
            delta_character=delta_character,
            delta_chassis=delta_chassis,
            reason=reason,
            confrontation_id=confrontation_id,
        )
    )

    return BondEventResult(
        tier_character_before=tier_char_before,
        tier_character_after=entry.bond_tier_character,
        tier_chassis_before=tier_chassis_before,
        tier_chassis_after=entry.bond_tier_chassis,
        tier_character_crossed=(tier_char_before != entry.bond_tier_character),
        tier_chassis_crossed=(tier_chassis_before != entry.bond_tier_chassis),
    )


def apply_chassis_lineage_intimate(
    *,
    chassis: ChassisInstance,
    narrative_seed: str,
    turn_id: int,
    confrontation_id: str | None,
) -> None:
    chassis.lineage.append(
        ChassisLineageEntry(
            turn_id=turn_id,
            kind="intimate",
            narrative_seed=narrative_seed,
            confrontation_id=confrontation_id,
        )
    )


# ---------------------------------------------------------------------------
# World-load wiring — fresh-session chassis_registry materialization
# ---------------------------------------------------------------------------


def rebind_chassis_bonds_to_character(snapshot: GameSnapshot, character_id: str) -> None:
    """Replace the ``"player_character"`` placeholder seed with the real
    chargen character id across every chassis in ``snapshot.chassis_registry``.

    Story 47-6: ``init_chassis_registry`` writes bond ledger seeds keyed
    against the placeholder ``"player_character"`` because chargen runs
    in a separate flow that doesn't have the chassis context. This
    function closes the loop — call it from chargen-complete (or from
    any session-start handler that has the real character id).

    Idempotent: a second call is a no-op for entries already keyed to a
    real id; only entries still keyed to the placeholder are rewritten.
    Multi-PC scenarios (deferred) will add new ledger entries rather
    than overwriting an existing real-id entry.
    """
    for chassis in snapshot.chassis_registry.values():
        for entry in chassis.bond_ledger:
            if entry.character_id == "player_character":
                entry.character_id = character_id


def init_chassis_registry(snapshot, genre_pack) -> None:
    """Load `worlds/<world_slug>/rigs.yaml` and materialize chassis state.

    Slice scope: fresh-session only. Returning-player save rehydration is
    deferred to a follow-on. Graceful no-op when:
      - pack.chassis_classes is None (genre doesn't use rigs)
      - pack.source_dir is None (in-memory pack with no on-disk YAML)
      - the world has no rigs.yaml authored

    Each chassis is added to ``snapshot.chassis_registry`` keyed by its id.
    Wave 2A (story 45-47) removed the projection into the legacy
    ``npc_registry`` (the registry was dropped in story 45-52); chassis
    surface in the narrator prompt via the dedicated
    ``register_chassis_voice_section`` (chassis voice zone, separate from
    the NPC roster).
    """
    import yaml

    from sidequest.genre.models.rigs_world import RigsWorldConfig

    if genre_pack.chassis_classes is None:
        return
    if genre_pack.source_dir is None:
        return

    rigs_path = genre_pack.source_dir / "worlds" / snapshot.world_slug / "rigs.yaml"
    if not rigs_path.exists():
        return

    raw = yaml.safe_load(rigs_path.read_text(encoding="utf-8"))
    cfg = RigsWorldConfig.model_validate(raw)

    # S1 step 2 — magic_state.confrontations is the canonical home for
    # confrontation defs. The legacy world_confrontations cache is gone;
    # this loader writes directly into magic_state. magic_state MUST be
    # initialized before init_chassis_registry runs (invariant established
    # by the snapshot split-brain cleanup, 2026-05-04). The bind path in
    # session_room is responsible for the ordering.
    confrontations_path = (
        genre_pack.source_dir / "worlds" / snapshot.world_slug / "confrontations.yaml"
    )
    if confrontations_path.exists():
        if snapshot.magic_state is None:
            raise RuntimeError(
                "init_chassis_registry: magic_state must be initialized before "
                "chassis registry when worlds/<world>/confrontations.yaml exists. "
                "Bind-path ordering invariant — see design spec "
                "2026-05-04-snapshot-split-brain-cleanup-design.md S1."
            )
        from sidequest.magic.confrontations import load_confrontations

        loaded = load_confrontations(confrontations_path)
        existing_ids = {c.id for c in snapshot.magic_state.confrontations}
        for cdef in loaded:
            if cdef.id not in existing_ids:
                snapshot.magic_state.confrontations.append(cdef)
                existing_ids.add(cdef.id)

    for inst_cfg in cfg.chassis_instances:
        bond_seeds = [
            BondLedgerEntry(
                # character_role placeholder ("player_character") gets
                # rewritten to the real chargen character id by
                # rebind_chassis_bonds_to_character() at session-start.
                character_id=seed.character_role,
                bond_strength_character_to_chassis=seed.bond_strength_character_to_chassis,
                bond_strength_chassis_to_character=seed.bond_strength_chassis_to_character,
                bond_tier_character=seed.bond_tier_character,
                bond_tier_chassis=seed.bond_tier_chassis,
            )
            for seed in inst_cfg.bond_seeds
        ]
        chassis = ChassisInstance(
            id=inst_cfg.id,
            name=inst_cfg.name,
            class_id=inst_cfg.chassis_class_id,
            OCEAN=inst_cfg.OCEAN,
            voice=inst_cfg.voice,
            interior_rooms=inst_cfg.interior_rooms,
            bond_ledger=bond_seeds,
        )
        snapshot.chassis_registry[chassis.id] = chassis
