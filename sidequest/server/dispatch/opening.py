"""Opening directive renderer + post-chargen resolution.

Replaces the old opening_hook.py. The unified Opening schema
(narrative.Opening) is composed against the world's chassis
instances, authored NPCs, magic register, and PC chargen choices
to produce a structured directive injected into the narrator's
Early zone for turn 1 only.

See docs/superpowers/specs/2026-05-01-canned-openings-design.md §2 + §6.
"""

from __future__ import annotations

import logging
import random

from sidequest.game.disposition import Disposition
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.chassis import BondTier
from sidequest.genre.models.narrative import (
    Opening,
    PerPcBeat,
)
from sidequest.genre.models.rigs_world import ChassisInstanceConfig
from sidequest.telemetry.spans import (
    SPAN_OPENING_DIRECTIVE_RENDERED,
    SPAN_OPENING_PLAYED,
    Span,
)

logger = logging.getLogger(__name__)


def _resolve_name_form(
    name_forms: dict[BondTier, str],
    tier: BondTier,
    *,
    first_name: str,
    last_name: str,
    nickname: str,
) -> str:
    template = name_forms.get(tier, "")
    return (
        template.replace("{first_name}", first_name)
        .replace("{last_name}", last_name)
        .replace("{nickname}", nickname or first_name)
    )


def _render_directive_chassis(
    *,
    opening: Opening,
    chassis: ChassisInstanceConfig,
    authored_crew: list[AuthoredNpc],
    magic_register: str,
    bond_tier_for_pc: BondTier,
    per_pc_beat: PerPcBeat | None,
    pc_first_name: str,
    pc_last_name: str,
    pc_nickname: str,
) -> str:
    """Render a chassis-anchored opening directive.

    `authored_crew` is the resolved list of AuthoredNpc objects whose
    ids match `chassis.crew_npcs` (caller does the lookup). Order
    matches the chassis declaration.
    """
    parts: list[str] = ["=== OPENING SCENARIO ==="]
    parts.append(f"Mode: {opening.triggers.mode}")
    if opening.name:
        parts.append(f"Title: {opening.name}")

    interior_room_label = chassis.interior_rooms[0]
    if opening.setting.interior_room and opening.setting.interior_room in chassis.interior_rooms:
        interior_room_label = opening.setting.interior_room
    parts.append(f"Setting: aboard the {chassis.name}, {interior_room_label}")
    if opening.setting.situation:
        parts.append(f"Situation: {opening.setting.situation}")

    parts.append("")
    parts.append(
        "ESTABLISHING NARRATION (Story 153-2: this is ALREADY shown to the "
        "player VERBATIM at cold-open — do NOT repeat or paraphrase it; continue "
        "the scene forward from where it leaves off):"
    )
    parts.append(opening.establishing_narration)

    if chassis.voice is not None:
        parts.append("")
        parts.append(f"CHASSIS VOICE (the {chassis.name} speaks):")
        name_form = _resolve_name_form(
            chassis.voice.name_forms_by_bond_tier,
            bond_tier_for_pc,
            first_name=pc_first_name,
            last_name=pc_last_name,
            nickname=pc_nickname,
        )
        parts.append(f'- Name-form for this PC at bond_tier {bond_tier_for_pc}: "{name_form}"')
        parts.append(f"- Default register: {chassis.voice.default_register}")
        if chassis.voice.vocal_tics:
            parts.append(f"- Vocal tics: {', '.join(chassis.voice.vocal_tics)}")
        if chassis.voice.silence_register:
            parts.append(f"- Silence register: {chassis.voice.silence_register}")
        if opening.rig_voice_seeds:
            for seed in opening.rig_voice_seeds:
                ctx = str(seed.get("context", "")).strip()
                line = str(seed.get("line", "")).strip()
                if ctx and line:
                    parts.append(f"- {ctx}: {line}")
                elif line:
                    parts.append(f"- {line}")

    if magic_register:
        parts.append("")
        parts.append("MAGIC REGISTER:")
        parts.append(magic_register)

    if opening.magic_microbleed is not None:
        parts.append("")
        parts.append("MICROBLEED (one quiet uncanny detail to weave in once):")
        parts.append(opening.magic_microbleed.detail)
        if opening.magic_microbleed.cost_bar:
            parts.append(f"- Tick {opening.magic_microbleed.cost_bar} by 0.05 via narration.")

    if authored_crew:
        parts.append("")
        parts.append(
            "PRE-LOADED NPCS PRESENT (already canonical in registry — when "
            "any of them is in the scene this turn, INCLUDE them in "
            "`npcs_present` so scene-presence and encounter routing know "
            "they're here. Do NOT redefine their role, pronouns, or "
            "appearance — those fields are authored. The server dedupes "
            "against the registry; you don't need to.):"
        )
        for npc in authored_crew:
            attitude = Disposition(npc.initial_disposition).attitude().value
            line = f"- {npc.name} ({npc.role}): {npc.appearance}, disposition: {attitude}"
            parts.append(line)
            if npc.history_seeds:
                first_seed = npc.history_seeds[0]
                parts.append(f"  History: {first_seed}")

    if per_pc_beat is not None:
        parts.append("")
        parts.append("PER-PC BEAT (textural moment for this PC's chargen):")
        parts.append(per_pc_beat.beat)

    if opening.tone.register or opening.tone.stakes or opening.tone.avoid_at_all_costs:
        parts.append("")
        parts.append("TONE:")
        if opening.tone.register:
            parts.append(f"- Register: {opening.tone.register}")
        if opening.tone.stakes:
            parts.append(f"- Stakes: {opening.tone.stakes}")
        if opening.tone.sensory_layers:
            parts.append(f"- Sensory layers: {opening.tone.sensory_layers}")
        if opening.tone.avoid_at_all_costs:
            parts.append("- AVOID: " + "; ".join(opening.tone.avoid_at_all_costs))

    if opening.soft_hook.narration:
        parts.append("")
        parts.append("SOFT HOOK (only when conversation lulls; otherwise wait turn 2 or 3):")
        parts.append(opening.soft_hook.narration)
        if opening.soft_hook.timing:
            parts.append(f"- Timing: {opening.soft_hook.timing}")
        for k, v in opening.soft_hook.escalation_path.items():
            parts.append(f"- Escalation/{k}: {v}")

    if opening.party_framing is not None:
        parts.append("")
        parts.append("PARTY FRAMING:")
        if opening.party_framing.already_a_crew:
            parts.append("- The PCs are already a crew. Do not re-introduce them.")
        parts.append(f"- Default bond tier: {opening.party_framing.bond_tier_default}")
        for seed in opening.party_framing.shared_history_seeds:
            parts.append(f"  • {seed}")
        if opening.party_framing.narrator_guidance:
            parts.append(f"- {opening.party_framing.narrator_guidance}")

    if opening.first_turn_invitation:
        parts.append("")
        parts.append("FIRST TURN INVITATION (close the scene on this — NO closing question):")
        parts.append(opening.first_turn_invitation)

    parts.append("")
    parts.append("=== END OPENING ===")
    return "\n".join(parts)


class OpeningResolutionError(Exception):
    """No opening in the world's bank matched the session's filters.

    Should be unreachable in practice — Validator 7+8 at world load
    guarantee at least one matching entry per (mode, background) tuple.
    Raised defensively when invariants are violated mid-flight.
    """


def _matches_mode(op_mode: str, requested: str) -> bool:
    if op_mode == "either":
        return True
    return op_mode == requested


def _matches_player_count(op: Opening, count: int) -> bool:
    return op.triggers.min_players <= count <= op.triggers.max_players


def _resolve_opening_post_chargen(
    bank: list[Opening],
    *,
    mode: str,
    player_count: int,
    pc_background: str,
    rng: random.Random | None = None,
    world_slug: str = "<unknown>",
) -> Opening:
    """Pick one Opening from the world's bank.

    Selection layers (in order):
    1. mode filter (solo/multiplayer + 'either' wildcard)
    2. player_count filter (min <= count <= max)
    3. background filter — keyed entries preferred over fallback
       (backgrounds=[] = fallback)
    4. seeded RNG choice among remaining candidates

    Raises ``OpeningResolutionError`` if no candidate matches —
    Validator 7+8 should make this unreachable.

    Emits ``opening.resolved`` on success, or ``opening.no_match``
    defensively when no candidate is available (CLAUDE.md "OTEL
    Observability Principle").

    See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md``
    §2.4.
    """
    from sidequest.telemetry.spans import (
        SPAN_OPENING_NO_MATCH,
        SPAN_OPENING_RESOLVED,
        Span,
    )

    rng = rng if rng is not None else random.Random()

    # Layers 1-2: mode + player_count
    pool = [op for op in bank if _matches_mode(op.triggers.mode, mode)]
    pool = [op for op in pool if _matches_player_count(op, player_count)]

    # Layer 3a: prefer background-keyed matches; 3b fall back to
    # backgrounds=[] entries when no keyed entry matches.
    keyed = [
        op for op in pool if op.triggers.backgrounds and pc_background in op.triggers.backgrounds
    ]
    candidates = keyed or [op for op in pool if not op.triggers.backgrounds]

    if not candidates:
        with Span.open(
            SPAN_OPENING_NO_MATCH,
            {
                "world_slug": world_slug,
                "mode": mode,
                "pc_background": pc_background,
                "player_count": player_count,
                "candidate_count": 0,
                "bank_size": len(bank),
            },
        ):
            pass
        raise OpeningResolutionError(
            f"World {world_slug!r}: no opening matches "
            f"(mode={mode}, player_count={player_count}, "
            f"pc_background={pc_background!r}). "
            "Validator 7+8 should have caught this at world load."
        )

    chosen = rng.choice(candidates)

    with Span.open(
        SPAN_OPENING_RESOLVED,
        {
            "world_slug": world_slug,
            "opening_id": chosen.id,
            "mode": mode,
            "player_count": player_count,
            "pc_background": pc_background,
            "candidates_count": len(candidates),
            "bank_size": len(bank),
        },
    ):
        pass

    return chosen


def _render_directive_location(
    *,
    opening: Opening,
    present_npcs: list[AuthoredNpc],
    magic_register: str,
    per_pc_beat: PerPcBeat | None,
) -> str:
    """Render a location-anchored opening directive (Aureate Span path)."""
    parts: list[str] = ["=== OPENING SCENARIO ==="]
    parts.append(f"Mode: {opening.triggers.mode}")
    if opening.name:
        parts.append(f"Title: {opening.name}")

    parts.append(f"Setting: at {opening.setting.location_label}")
    if opening.setting.situation:
        parts.append(f"Situation: {opening.setting.situation}")

    parts.append("")
    parts.append(
        "ESTABLISHING NARRATION (Story 153-2: this is ALREADY shown to the "
        "player VERBATIM at cold-open — do NOT repeat or paraphrase it; continue "
        "the scene forward from where it leaves off):"
    )
    parts.append(opening.establishing_narration)

    if magic_register:
        parts.append("")
        parts.append("MAGIC REGISTER:")
        parts.append(magic_register)

    if opening.magic_microbleed is not None:
        parts.append("")
        parts.append("MICROBLEED (one quiet uncanny detail to weave in once):")
        parts.append(opening.magic_microbleed.detail)

    if present_npcs:
        parts.append("")
        parts.append(
            "PRE-LOADED NPCS PRESENT (already canonical in registry — when "
            "any of them is in the scene this turn, INCLUDE them in "
            "`npcs_present` so scene-presence and encounter routing know "
            "they're here. Do NOT redefine their role, pronouns, or "
            "appearance — those fields are authored. The server dedupes "
            "against the registry; you don't need to.):"
        )
        for npc in present_npcs:
            attitude = Disposition(npc.initial_disposition).attitude().value
            parts.append(f"- {npc.name} ({npc.role}): {npc.appearance}, disposition: {attitude}")
            if npc.history_seeds:
                parts.append(f"  History: {npc.history_seeds[0]}")

    if per_pc_beat is not None:
        parts.append("")
        parts.append("PER-PC BEAT (textural moment for this PC's chargen):")
        parts.append(per_pc_beat.beat)

    if opening.tone.register or opening.tone.avoid_at_all_costs:
        parts.append("")
        parts.append("TONE:")
        if opening.tone.register:
            parts.append(f"- Register: {opening.tone.register}")
        if opening.tone.stakes:
            parts.append(f"- Stakes: {opening.tone.stakes}")
        if opening.tone.avoid_at_all_costs:
            parts.append("- AVOID: " + "; ".join(opening.tone.avoid_at_all_costs))

    if opening.soft_hook.narration:
        parts.append("")
        parts.append("SOFT HOOK (only when conversation lulls; otherwise wait turn 2 or 3):")
        parts.append(opening.soft_hook.narration)

    if opening.party_framing is not None:
        parts.append("")
        parts.append("PARTY FRAMING:")
        if opening.party_framing.already_a_crew:
            parts.append("- The PCs are already a crew. Do not re-introduce them.")
        parts.append(f"- Default bond tier: {opening.party_framing.bond_tier_default}")
        if opening.party_framing.narrator_guidance:
            parts.append(f"- {opening.party_framing.narrator_guidance}")

    if opening.first_turn_invitation:
        parts.append("")
        parts.append("FIRST TURN INVITATION (close the scene on this — NO closing question):")
        parts.append(opening.first_turn_invitation)

    parts.append("")
    parts.append("=== END OPENING ===")
    return "\n".join(parts)


def build_directive(
    *,
    opening: Opening,
    chassis: ChassisInstanceConfig | None,
    authored_crew: list[AuthoredNpc],
    magic_register: str,
    bond_tier_for_pc: BondTier,
    per_pc_beat: PerPcBeat | None,
    pc_first_name: str,
    pc_last_name: str,
    pc_nickname: str,
    present_npcs: list[AuthoredNpc],
) -> str:
    """Top-level renderer dispatch — picks chassis or location path
    based on the Opening's setting anchor.

    Emits ``opening.directive_rendered`` with anchor + content metrics
    so the GM panel can verify the directive made it to the narrator
    (CLAUDE.md "OTEL Observability Principle").

    See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md``
    §3.3.
    """
    if opening.setting.chassis_instance is not None and chassis is not None:
        directive = _render_directive_chassis(
            opening=opening,
            chassis=chassis,
            authored_crew=authored_crew,
            magic_register=magic_register,
            bond_tier_for_pc=bond_tier_for_pc,
            per_pc_beat=per_pc_beat,
            pc_first_name=pc_first_name,
            pc_last_name=pc_last_name,
            pc_nickname=pc_nickname,
        )
    else:
        directive = _render_directive_location(
            opening=opening,
            present_npcs=present_npcs,
            magic_register=magic_register,
            per_pc_beat=per_pc_beat,
        )

    with Span.open(
        SPAN_OPENING_DIRECTIVE_RENDERED,
        {
            "opening_id": opening.id,
            "char_count": len(directive),
            "anchor": "chassis" if opening.setting.chassis_instance else "location",
            "has_microbleed": opening.magic_microbleed is not None,
            "has_party_framing": opening.party_framing is not None,
            "crew_count": len(authored_crew),
            "present_npc_count": len(present_npcs),
        },
    ):
        pass

    return directive


def record_opening_played(
    *,
    opening_id: str,
    turn_id: int,
) -> None:
    """Emit ``opening.played`` at first-turn consumption.

    Caller (websocket_session_handler) invokes this after the directive
    is consumed and cleared from session_data, so the GM panel can
    confirm the canned opening actually reached the narrator's first
    turn rather than being silently dropped.
    """
    with Span.open(
        SPAN_OPENING_PLAYED,
        {
            "opening_id": opening_id,
            "turn_id": turn_id,
        },
    ):
        pass
