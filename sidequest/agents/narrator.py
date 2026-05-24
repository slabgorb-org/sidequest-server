"""Narrator agent — handles exploration, description, and story progression.

Port of sidequest-agents/src/agents/narrator.rs.
Refactored in story 23-1: hardcoded NARRATOR_SYSTEM_PROMPT replaced with
structured template sections across attention zones.

ADR-067: Unified narrator agent. Combat, chase, and dialogue handling absorbed
from former separate agents (CreatureSmith, Dialectician, Ensemble).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sidequest.game.encounter import StructuredEncounter
    from sidequest.genre.models.character import ClassDef
    from sidequest.genre.models.rules import ConfrontationDef

from sidequest.agents.agent import BaseAgent
from sidequest.agents.narrator_prompts import (
    NARRATOR_AGENCY,
    NARRATOR_CHASE_RULES,
    NARRATOR_COMBAT_RULES,
    NARRATOR_CONSEQUENCES,
    NARRATOR_CONSTRAINTS,
    NARRATOR_DIALOGUE_RULES,
    NARRATOR_IDENTITY,
    NARRATOR_OUTPUT_ONLY,
    NARRATOR_OUTPUT_STYLE,
    NARRATOR_POV_RULES,
    NARRATOR_REFERRAL_RULE,
)
from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    PromptSection,
    SectionCategory,
)
from sidequest.game.trope_time_skip import TimeSkipBeatEvent

# Prompt section text lives in sidequest/agents/narrator_prompts/*.md and is
# re-exported here so existing imports (tests, orchestrator) keep working.
__all__ = [
    "NARRATOR_IDENTITY",
    "NARRATOR_CONSTRAINTS",
    "NARRATOR_AGENCY",
    "NARRATOR_CONSEQUENCES",
    "NARRATOR_OUTPUT_ONLY",
    "NARRATOR_OUTPUT_STYLE",
    "NARRATOR_REFERRAL_RULE",
    "NARRATOR_COMBAT_RULES",
    "NARRATOR_CHASE_RULES",
    "NARRATOR_DIALOGUE_RULES",
    "NARRATOR_POV_RULES",
    "NarratorAgent",
    "narrator_output_format_text",
    "is_streaming_enabled",
    "_render_time_skip_context",
]


def narrator_output_format_text() -> str:
    """Returns the NARRATOR_OUTPUT_ONLY prompt section text.

    Used by integration tests and CLI prompt inspection tools.
    Port of narrator_output_format_text() in narrator.rs.
    """
    return NARRATOR_OUTPUT_ONLY


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def is_streaming_enabled() -> bool:
    """True when the narrator should use the streaming claude_client path.

    Gated by SIDEQUEST_NARRATOR_STREAMING env var. Default off to preserve
    existing synchronous behavior until the full streaming pipeline ships.
    """
    return os.environ.get("SIDEQUEST_NARRATOR_STREAMING", "0") == "1"


# ---------------------------------------------------------------------------
# Story 50-4 — TIME-SKIP CONTEXT block renderer
# ---------------------------------------------------------------------------


def _render_time_skip_context(
    summary: list[TimeSkipBeatEvent],
    days_elapsed_total: int,
) -> str:
    """Story 50-4 — render the TIME-SKIP CONTEXT block for the next narrator turn.

    Called when ``snapshot.pending_time_skip_summary`` is non-empty. After
    rendering, the caller MUST clear ``snapshot.pending_time_skip_summary``
    (one-shot lifecycle).

    Returns an empty string if the summary is empty — the caller skips
    registering the section in that case (zero-byte-leak / no phantom header).
    """
    if not summary:
        return ""
    skip_total = max(entry.days_into_skip for entry in summary)
    lines = [
        "## TIME-SKIP CONTEXT",
        "",
        (
            f"The previous narration advanced time by {skip_total} in-game days "
            f"(total elapsed: {days_elapsed_total}). The following developed off-screen "
            "during that span. Weave these into your next narration as has-already-happened "
            "context — the players are arriving INTO this changed state, not witnessing it unfold."
        ),
        "",
    ]
    for entry in summary:
        npcs = ", ".join(entry.npcs_involved) if entry.npcs_involved else "—"
        lines.append(
            f"- Day {entry.days_into_skip} — {entry.trope_id} — "
            f'"{entry.beat_event}" (stakes: {entry.stakes}; npcs: {npcs})'
        )
    lines.append("")
    lines.append(
        "Acknowledge the time passage. Reference the most impactful items by stakes. "
        "You do not need to cite all beats — pick what serves the scene. "
        "Do NOT contradict any beat that fired."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# NarratorAgent
# ---------------------------------------------------------------------------


class NarratorAgent(BaseAgent):
    """The exploration/narration agent — drives story progression, world description,
    NPC dialogue, and patch emission. Routed to as the default agent (per ADR-067).

    Port of sidequest-agents/src/agents/narrator.rs::NarratorAgent.
    """

    def name(self) -> str:
        return "narrator"

    def system_prompt(self) -> str:
        return NARRATOR_IDENTITY

    def build_context(self, registry: object) -> None:
        """Add narrator-specific sections to a PromptRegistry.

        Port of NarratorAgent::build_context() in narrator.rs.
        Sections match the Rust implementation's zone/category assignments.
        """
        from sidequest.agents.prompt_framework.core import PromptRegistry

        if not isinstance(registry, PromptRegistry):
            raise TypeError(f"Expected PromptRegistry, got {type(registry)}")

        # Primacy/Identity — narrator core identity
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_identity",
                f"<identity>\n{NARRATOR_IDENTITY}\n</identity>",
                AttentionZone.Primacy,
                SectionCategory.Identity,
            ),
        )

        # Primacy/Guardrail — silent constraint handling
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_constraints",
                f"<critical>\n{NARRATOR_CONSTRAINTS}\n</critical>",
                AttentionZone.Primacy,
                SectionCategory.Guardrail,
            ),
        )

        # Primacy/Guardrail — agency (including multiplayer)
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_agency",
                f"<critical>\n{NARRATOR_AGENCY}\n</critical>",
                AttentionZone.Primacy,
                SectionCategory.Guardrail,
            ),
        )

        # Primacy/Guardrail — POV / pronoun discipline for the per-recipient
        # name-driven rewriter (sq-playtest 2026-05-23: pov_swap pronoun
        # passes retired after the "You doesn't hurry" antecedent-blindness
        # bug; the narrator-side discipline of using the PC's NAME instead
        # of pronouns is what keeps NPC pronouns out of the swap surface).
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_pov_rules",
                f"<critical>\n{NARRATOR_POV_RULES}\n</critical>",
                AttentionZone.Primacy,
                SectionCategory.Guardrail,
            ),
        )

        # Primacy/Guardrail — consequences follow genre tone
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_consequences",
                f"<critical>\n{NARRATOR_CONSEQUENCES}\n</critical>",
                AttentionZone.Primacy,
                SectionCategory.Guardrail,
            ),
        )

        # narrator_output_only is injected via build_output_format(), called
        # exactly once from Orchestrator.build_narrator_prompt (ADR-098
        # removed Full/Delta tiering — there is no per-tier builder). That
        # single call is backend-gated: the claude -p path gets the legacy
        # full-sidecar prose; the SDK tool-use path gets the slimmed-sidecar
        # + tool-routing prose. See NarratorAgent.build_output_format.

        # Early/Format — output-style rules
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_output_style",
                f"<output-style>\n{NARRATOR_OUTPUT_STYLE}\n</output-style>",
                AttentionZone.Early,
                SectionCategory.Format,
            ),
        )

        # Early/Guardrail — referral rule (not in SOUL.md)
        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_referral_rule",
                f"<important>\n{NARRATOR_REFERRAL_RULE}\n</important>",
                AttentionZone.Early,
                SectionCategory.Guardrail,
            ),
        )

    def build_output_format(self, registry: object) -> None:
        """Inject the game_patch output-format spec (SDK tool-use prose).

        Called exactly once from ``Orchestrator.build_narrator_prompt``
        (ADR-098 removed Full/Delta tiering). The section is always named
        ``narrator_output_only`` and always lands in the Primacy/Guardrail
        zone.

        Story 61-9 retired the legacy ``claude -p`` / Ollama narrator path
        per ADR-101 amendment; the SDK prose at ``NARRATOR_OUTPUT_ONLY``
        is the only output-format prose. ``llm_factory.build_llm_client``
        fails loud at construction if a non-SDK backend is selected — no
        runtime backend gate here.
        """
        from sidequest.agents.prompt_framework.core import PromptRegistry

        if not isinstance(registry, PromptRegistry):
            raise TypeError(f"Expected PromptRegistry, got {type(registry)}")

        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_output_only",
                f"<critical>\n{NARRATOR_OUTPUT_ONLY}\n</critical>",
                AttentionZone.Primacy,
                SectionCategory.Guardrail,
            ),
        )

    def build_encounter_context(
        self,
        registry: object,
        *,
        encounter: StructuredEncounter | None = None,
        cdef: ConfrontationDef | None = None,
        encounter_summary: str | None = None,
        statuses_by_actor: dict[str, list] | None = None,
        resolution_signal: object | None = None,
        pc_classes_by_name: dict[str, tuple[ClassDef, float]] | None = None,
    ) -> None:
        """Inject encounter-specific narration rules + live encounter state.

        When ``encounter`` and ``cdef`` are given, render:
        1. The generic encounter-rules prose (unchanged — backwards compatible).
        2. The matched ConfrontationDef's beats + actors so the LLM emits
           valid ``beat_selections``.
        3. Both ascending dials (player_metric / opponent_metric).
        4. Per-actor statuses from ``statuses_by_actor``.
        5. Encounter tags.

        When ``resolution_signal`` is set, short-circuits to the one-shot
        [ENCOUNTER RESOLVED] zone and skips the live zone entirely.

        Port of NarratorAgent::build_encounter_context() in narrator.rs.
        Task 18: dual-dial encounter zone + resolution signal short-circuit.
        """
        from sidequest.agents.prompt_framework.core import PromptRegistry

        if not isinstance(registry, PromptRegistry):
            raise TypeError(f"Expected PromptRegistry, got {type(registry)}")

        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_encounter_rules",
                f"<encounter-rules>\n{NARRATOR_COMBAT_RULES}\n"
                f"{NARRATOR_CHASE_RULES}\n</encounter-rules>",
                AttentionZone.Early,
                SectionCategory.Guardrail,
            ),
        )

        # One-shot resolution zone — wins over the active zone.
        if resolution_signal is not None:
            body = (
                "[ENCOUNTER RESOLVED]\n"
                f"type: {resolution_signal.encounter_type}\n"
                f"outcome: {resolution_signal.outcome}\n"
                f"final_player_metric: {resolution_signal.final_player_metric}\n"
                f"final_opponent_metric: {resolution_signal.final_opponent_metric}\n"
            )
            if resolution_signal.outcome == "yielded":
                yielded = ", ".join(resolution_signal.yielded_actors) or "(none)"
                body += (
                    f"yielded_actors: [{yielded}]\n"
                    f"edge_refreshed: {resolution_signal.edge_refreshed}\n"
                    "Describe the actor's exit on their own terms — they chose "
                    "to leave. Honor the choice. The opposing side does not "
                    "pursue or strike further.\n"
                )
            else:
                body += (
                    "The encounter has ended this turn. Describe the resolution "
                    "and any immediate transition out of the scene. Do NOT emit "
                    "beat_selections. Do NOT continue describing the encounter "
                    "as if it were active.\n"
                )
            registry.register_section(
                self.name(),
                PromptSection.new(
                    "narrator_encounter_resolved",
                    body,
                    AttentionZone.Early,
                    SectionCategory.State,
                ),
            )
            return  # short-circuit: do not render the live zone

        if encounter is not None and cdef is not None:
            statuses_by_actor = statuses_by_actor or {}
            actor_lines: list[str] = []
            for a in encounter.actors:
                statuses = statuses_by_actor.get(a.name, [])
                status_text = (
                    f"statuses: [{', '.join(f'{s.text} ({s.severity.value})' for s in statuses)}]"
                    if statuses
                    else "statuses: []"
                )
                actor_lines.append(f"  - {a.name} (side={a.side}, {status_text})")
            # All-beats listing — used by the narrator to pick OPPONENT beats
            # (the engine doesn't filter opponent options by class, since
            # NPCs/monsters resolve via opponent_default_stats / per-actor stats,
            # and any pack beat is fair game for the narrator to assign them).
            beat_lines = "\n".join(
                f"  - {b.id}: {b.label} (kind={b.kind.value}, base={b.base})" for b in cdef.beats
            )

            # Per-PC class-filtered beat menus (Task 7 of C&C B/X class beats).
            # When ``pc_classes_by_name`` is supplied, render a "X (Name) can:"
            # line for each PC actor showing only the beats their class can
            # legally pick this turn. Class filter chain lives in
            # ``sidequest.game.beat_filter.beats_available_for`` — single source
            # of truth so future B/X memorization (story #2) plugs in there,
            # not into prompt rendering. The all-beats block above stays for
            # opponent-side selection.
            pc_beat_lines = ""
            if pc_classes_by_name:
                from sidequest.game.beat_filter import (
                    beats_available_for,
                    cast_spell_rejection_reason,
                )
                from sidequest.telemetry.spans import confrontation_beat_filter_span

                pc_blocks: list[str] = []
                for actor in encounter.actors:
                    if actor.side != "player":
                        continue
                    entry = pc_classes_by_name.get(actor.name)
                    if entry is None:
                        continue
                    # Story 47-10: pc_classes_by_name entry is (class_def,
                    # spell_slots, prepared_spells). Older callers may still
                    # ship the 2-tuple shape — tolerate both.
                    if len(entry) == 3:
                        class_def, spell_slots, prepared_spells = entry
                    else:
                        class_def, spell_slots = entry
                        prepared_spells = None
                    available = beats_available_for(
                        cdef,
                        class_def,
                        spell_slots_remaining=spell_slots,
                        prepared_spells=prepared_spells,
                    )
                    available_ids = [b.id for b in available]
                    ids = ", ".join(available_ids) or "(none)"
                    pc_blocks.append(f"  - {class_def.display_name} ({actor.name}) can: {ids}")
                    # Story 47-10: when cast_spell is filtered out, surface
                    # the precise rejection reason on the OTEL span so the GM
                    # panel can distinguish "out of slots" from "didn't
                    # memorize anything" — Sebastien-tier observability.
                    rejection_reason = cast_spell_rejection_reason(
                        cdef,
                        class_def,
                        spell_slots_remaining=spell_slots,
                        prepared_spells=prepared_spells,
                    )
                    # OTEL: GM-panel verifies the filter is wired, not just
                    # defined (CLAUDE.md OTEL-on-every-subsystem).
                    span_kwargs: dict[str, Any] = {
                        "actor": actor.name,
                        "class_name": class_def.display_name,
                        "confrontation_type": cdef.confrontation_type,
                        "available_beat_ids": ",".join(available_ids),
                        "spell_slots_remaining": spell_slots,
                        "pool_size": len(cdef.beats),
                        "filtered_size": len(available),
                        # Story 49-7: discriminate this emit from the panel-
                        # projection one at dispatch/confrontation.py so the
                        # GM panel renders the two filter runs separately.
                        "source": "narrator_prompt",
                    }
                    if rejection_reason is not None:
                        span_kwargs["cast_spell_rejection_reason"] = rejection_reason
                    with confrontation_beat_filter_span(**span_kwargs):
                        pass
                if pc_blocks:
                    pc_beat_lines = (
                        "Player-character beat menus — each PC's "
                        "beat_selection.beat_id MUST come from THEIR class's "
                        "list (cross-class picks are illegal):\n" + "\n".join(pc_blocks) + "\n"
                        "The player's available actions for this turn are listed above. "
                        "Do not narrate actions outside that list as performed.\n"
                    )
            tag_lines = (
                "\n".join(
                    f'  - "{t.text}" on {t.target or "(scene)"} '
                    f"({'fleeting' if t.fleeting else f'leverage {t.leverage}'}, "
                    f"created turn {t.created_turn})"
                    for t in encounter.tags
                )
                or "  (none)"
            )
            # Resolution-mode gate (combat fairness, 2026-04-26).
            # When the active confrontation is opposed_check, the engine
            # rolls dice for both sides and derives the outcome tier from
            # the shift between rolls. The narrator's job is to PICK
            # WHICH BEAT the opponent took (which action), but never the
            # outcome tier — that comes from the resolver. Without this
            # explicit gate the LLM tends to write "the orc swings and
            # connects, opening a gash on Sam's arm" (i.e. embeds an
            # outcome) which makes the engine-derived tier inconsistent
            # with the prose. See:
            # ``.archive/handoffs/opposed-checks-design.md``.
            from sidequest.genre.models.rules import ResolutionMode

            opposed_gate_text = ""
            if cdef.resolution_mode == ResolutionMode.opposed_check:
                opposed_gate_text = (
                    "RESOLUTION_MODE: opposed_check\n"
                    "When the active confrontation has resolution_mode: "
                    "opposed_check, you select only the OPPONENT'S BEAT "
                    "(which action). The engine rolls dice and derives "
                    "the outcome tier from the shift between your roll "
                    "and the player's. You DO NOT specify outcome tier "
                    "for either side — the dice decide. Describe the "
                    "opponent's action as it begins; do not narrate "
                    "whether it lands or fails until the engine returns "
                    "the resolved tier on the next turn.\n"
                )
            body = (
                f"<encounter-live>\n"
                f"Active encounter: {cdef.label} ({cdef.confrontation_type})\n"
                f"{opposed_gate_text}"
                f"Player metric: {encounter.player_metric.current} / "
                f"{encounter.player_metric.threshold}\n"
                f"Opponent metric: {encounter.opponent_metric.current} / "
                f"{encounter.opponent_metric.threshold}\n"
                f"Available beats — beat_selections.beat_id MUST be one of:\n"
                f"{beat_lines}\n"
                f"{pc_beat_lines}"
                f"Actors — emit a beat_selection for every non-withdrawn "
                f"non-neutral actor:\n" + "\n".join(actor_lines) + "\n"
                f"Encounter tags:\n{tag_lines}\n"
                f"</encounter-live>"
            )
            registry.register_section(
                self.name(),
                PromptSection.new(
                    "narrator_encounter_live",
                    body,
                    AttentionZone.Early,
                    SectionCategory.State,
                ),
            )

        if encounter_summary:
            registry.register_section(
                self.name(),
                PromptSection.new(
                    "narrator_encounter_summary",
                    f"<encounter-state>\n{encounter_summary}\n</encounter-state>",
                    AttentionZone.Valley,
                    SectionCategory.State,
                ),
            )

    def build_dialogue_context(self, registry: object) -> None:
        """Inject dialogue-specific narration rules into the prompt (ADR-067).

        Called by the orchestrator when NPCs are present or dialogue is likely.

        Port of NarratorAgent::build_dialogue_context() in narrator.rs.
        """
        from sidequest.agents.prompt_framework.core import PromptRegistry

        if not isinstance(registry, PromptRegistry):
            raise TypeError(f"Expected PromptRegistry, got {type(registry)}")

        registry.register_section(
            self.name(),
            PromptSection.new(
                "narrator_dialogue_rules",
                f"<dialogue-rules>\n{NARRATOR_DIALOGUE_RULES}\n</dialogue-rules>",
                AttentionZone.Early,
                SectionCategory.Guardrail,
            ),
        )
