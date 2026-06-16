"""Prompt framework core — PromptComposer protocol and PromptRegistry implementation.

Port of sidequest-agents/src/prompt_framework/mod.rs (everything beyond types + soul).

PromptComposer is the abstract protocol (Python Protocol class, analogous to the
Rust trait). PromptRegistry is the concrete implementation that stores sections
per-agent and composes them in attention-zone order.

Note: Methods that depend on sidequest.game types (register_ocean_personalities_section,
register_ability_context, register_knowledge_section, register_resource_section,
register_pacing_section, register_scene_directive) are included but only those
types already ported in Phase 1 are used.  The remaining helpers that reference
sidequest_game types not yet in Phase 1 (e.g. SceneDirective, KnownFact, Npc)
are gated by TYPE_CHECKING so the registry is still importable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    PromptSection,
    SectionCategory,
)

if TYPE_CHECKING:
    from sidequest.dungeon.region_projection import RegionProjection
    from sidequest.game.chassis import ChassisInstance
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.game.session import Npc, PartyPeer


# ---------------------------------------------------------------------------
# Protocol (Rust trait → Python Protocol)
# ---------------------------------------------------------------------------


@runtime_checkable
class PromptComposer(Protocol):
    """Protocol for assembling prompt sections into a final prompt string.

    Implementors register sections and compose them in attention-optimal zone order.
    Port of the Rust PromptComposer trait.
    """

    def register_section(self, agent_name: str, section: PromptSection) -> None:
        """Register a section for a given agent."""
        ...

    def registry(self, agent_name: str) -> list[PromptSection]:
        """Return sections for an agent in insertion order."""
        ...

    def get_sections(
        self,
        agent_name: str,
        category: SectionCategory | None = None,
        zone: AttentionZone | None = None,
    ) -> list[PromptSection]:
        """Return sections filtered by optional category and/or zone."""
        ...

    def compose(self, agent_name: str) -> str:
        """Compose all registered sections for an agent into a final prompt string."""
        ...

    def clear(self, agent_name: str) -> None:
        """Clear all sections for an agent."""
        ...


# ---------------------------------------------------------------------------
# Agents that receive pacing/verbosity guidance (post ADR-067: only narrator)
# ---------------------------------------------------------------------------

_PACING_AGENTS: frozenset[str] = frozenset(["narrator"])
_NARRATING_AGENTS: frozenset[str] = frozenset(["narrator"])


def _split_player_name(name: str) -> tuple[str, str]:
    """Split a display name into (first, last). Single-token names get last="".

    Slice-scope helper for chassis voice section. Real chargen rebind lands later.
    """
    parts = name.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# PromptRegistry — concrete PromptComposer implementation
# ---------------------------------------------------------------------------


class PromptRegistry:
    """Concrete implementation of PromptComposer.

    Stores sections per agent, composes in zone order.
    Port of sidequest-agents PromptRegistry.
    """

    def __init__(self) -> None:
        self._sections: dict[str, list[PromptSection]] = {}

    # ------------------------------------------------------------------
    # PromptComposer protocol implementation
    # ------------------------------------------------------------------

    def register_section(self, agent_name: str, section: PromptSection) -> None:
        """Register a section for a given agent."""
        self._sections.setdefault(agent_name, []).append(section)

    def registry(self, agent_name: str) -> list[PromptSection]:
        """Return sections for an agent in insertion order."""
        return list(self._sections.get(agent_name, []))

    def get_sections(
        self,
        agent_name: str,
        category: SectionCategory | None = None,
        zone: AttentionZone | None = None,
    ) -> list[PromptSection]:
        """Return sections filtered by optional category and/or zone."""
        sections = self._sections.get(agent_name, [])
        result = []
        for s in sections:
            if category is not None and s.category != category:
                continue
            if zone is not None and s.zone != zone:
                continue
            result.append(s)
        return result

    def compose(self, agent_name: str) -> str:
        """Compose all registered sections for an agent into a final prompt string.

        Sorts by zone order before joining — same as Rust implementation.
        """
        from sidequest.telemetry.spans import SPAN_COMPOSE, Span

        with Span.open(SPAN_COMPOSE, {"agent_name": agent_name}) as span:
            sections = list(self._sections.get(agent_name, []))
            sections.sort(key=lambda s: s.zone.order())
            non_empty = [s.content for s in sections if not s.is_empty()]
            rendered = "\n\n".join(non_empty)
            span.set_attribute("section_count", len(sections))
            span.set_attribute("non_empty_count", len(non_empty))
            span.set_attribute("rendered_chars", len(rendered))
            return rendered

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        """Compose registered sections into a (system_prompt, user_message) pair.

        Partitions sections by :func:`default_bucket_for_section` keyed on
        section name. Within each bucket, sections are emitted in zone
        order (Primacy → Early → Valley → Late → Recency) and joined
        with double-newlines — same shape as :meth:`compose`.

        Used by :class:`Orchestrator` for stateless narrator turns
        (ADR-098). The system prompt carries the stable scaffold; the
        user message carries turn-dynamic state plus the player's action.
        """
        from sidequest.agents.prompt_framework.bucket import (
            SectionBucket,
            default_bucket_for_section,
        )
        from sidequest.telemetry.spans import SPAN_COMPOSE, Span

        with Span.open(SPAN_COMPOSE, {"agent_name": agent_name, "split": True}) as span:
            sections = list(self._sections.get(agent_name, []))
            sections.sort(key=lambda s: s.zone.order())

            system_parts: list[str] = []
            user_parts: list[str] = []
            for s in sections:
                if s.is_empty():
                    continue
                bucket = default_bucket_for_section(s.name)
                if bucket == SectionBucket.System:
                    system_parts.append(s.content)
                else:
                    user_parts.append(s.content)

            system_text = "\n\n".join(system_parts)
            user_text = "\n\n".join(user_parts)
            span.set_attribute("system_chars", len(system_text))
            span.set_attribute("user_chars", len(user_text))
            span.set_attribute("system_section_count", len(system_parts))
            span.set_attribute("user_section_count", len(user_parts))
            return system_text, user_text

    def compose_split_by_zone(self, agent_name: str) -> tuple[dict[AttentionZone, str], str]:
        """Compose system-bucket sections grouped by attention zone.

        Returns ``(system_by_zone, user_text)`` where ``system_by_zone``
        maps each :class:`AttentionZone` to the joined text of
        system-bucket sections in that zone, and ``user_text`` is the
        joined text of all user-bucket sections (zone-sorted, same as
        :meth:`compose_split`).

        Used by :class:`Orchestrator` to assemble multiple
        :class:`CacheableBlock` entries by zone (ADR-101 Phase D Task 6):
        Primacy + Early go in the cached block, Valley and Late + Recency
        ride uncached blocks so they may mutate per turn without breaking
        the cache prefix.

        Empty zones are omitted from the dict. Empty user content yields
        an empty string. Section content for any zone is byte-stable
        across calls given identical registered sections — see the
        :func:`compose_split`-driven byte-stability test in
        ``tests/agents/test_cache_ttl_prefix_and_otel.py`` for the
        load-bearing guarantee.
        """
        from sidequest.agents.prompt_framework.bucket import (
            SectionBucket,
            default_bucket_for_section,
        )
        from sidequest.telemetry.spans import SPAN_COMPOSE, Span

        with Span.open(
            SPAN_COMPOSE, {"agent_name": agent_name, "split": True, "by_zone": True}
        ) as span:
            sections = list(self._sections.get(agent_name, []))
            sections.sort(key=lambda s: s.zone.order())

            system_by_zone: dict[AttentionZone, list[str]] = {}
            user_parts: list[str] = []
            for s in sections:
                if s.is_empty():
                    continue
                bucket = default_bucket_for_section(s.name)
                if bucket == SectionBucket.System:
                    system_by_zone.setdefault(s.zone, []).append(s.content)
                else:
                    user_parts.append(s.content)

            zone_text: dict[AttentionZone, str] = {
                zone: "\n\n".join(parts) for zone, parts in system_by_zone.items()
            }
            user_text = "\n\n".join(user_parts)
            span.set_attribute("user_chars", len(user_text))
            for zone, text in zone_text.items():
                span.set_attribute(f"system_chars.{zone.value}", len(text))
            return zone_text, user_text

    def clear(self, agent_name: str) -> None:
        """Clear all sections for an agent."""
        self._sections.pop(agent_name, None)

    def render_for(self, agent_name: str) -> str:
        """Alias for compose — returns the composed prompt for the named agent.

        Task 18 (dual-track momentum): test helpers use this name to keep
        the assertion side readable ("what the narrator sees").
        """
        return self.compose(agent_name)

    # ------------------------------------------------------------------
    # High-level registration helpers (port of PromptRegistry impl block)
    # ------------------------------------------------------------------

    def register_pacing_section(
        self,
        agent_name: str,
        narrator_directive: str,
        escalation_beat: str | None = None,
    ) -> None:
        """Inject pacing guidance into the prompt for narrating agents.

        Non-narrating agents are silently skipped.
        """
        if agent_name not in _PACING_AGENTS:
            return

        content = f"## Pacing Guidance\n{narrator_directive}"
        if escalation_beat:
            content += f"\n\n## Escalation Beat\n{escalation_beat}"

        self.register_section(
            agent_name,
            PromptSection.new(
                "pacing",
                content,
                AttentionZone.Late,
                SectionCategory.Context,
            ),
        )

    def register_verbosity_section(self, agent_name: str, verbosity: str) -> None:
        """Inject narrator verbosity instructions into the system prompt.

        Only applies to narrating agents. Non-narrating agents are silently skipped.
        Story 14-3: Per-session verbosity control.

        verbosity: one of 'concise', 'standard', 'verbose'
        """
        if agent_name not in _NARRATING_AGENTS:
            return

        _VERBOSITY_MAP: dict[str, str] = {
            "concise": (
                "<critical>\n"
                "<length-limit>\n"
                "HARD LIMIT: Maximum 4 sentences of prose. DO NOT EXCEED 400 characters of narrative text.\n"
                "This overrides all other length guidance. If a trope beat or genre instruction "
                "would push you past this limit, cut description — never cut the limit.\n"
                "Action and consequence only. No atmosphere. No sensory detail.\n"
                "The game_patch JSON does not count toward this limit.\n"
                "</length-limit>\n"
                "</critical>"
            ),
            "verbose": (
                "<critical>\n"
                "<length-limit>\n"
                "HARD LIMIT: Maximum 10 sentences of prose. DO NOT EXCEED 1000 characters of narrative text.\n"
                "This overrides all other length guidance. If a trope beat or genre instruction "
                "would push you past this limit, cut description — never cut the limit.\n"
                "Rich atmosphere for arrivals and reveals. Shorter for simple actions.\n"
                "The game_patch JSON does not count toward this limit.\n"
                "</length-limit>\n"
                "</critical>"
            ),
        }
        # Default (standard) or unknown values fall back to standard.
        content = _VERBOSITY_MAP.get(
            verbosity,
            (
                "<critical>\n"
                "<length-limit>\n"
                "HARD LIMIT: Maximum 6 sentences of prose. DO NOT EXCEED 600 characters of narrative text.\n"
                "This overrides all other length guidance. If a trope beat, genre voice instruction, "
                "or MUST-weave directive would push you past this limit, cut description — never cut the limit.\n"
                "One short paragraph for simple actions. Two short paragraphs for arrivals or reveals.\n"
                "The game_patch JSON block does not count toward this limit.\n"
                "Count your sentences before responding. If you have more than 6, cut.\n"
                "</length-limit>\n"
                "</critical>"
            ),
        )

        self.register_section(
            agent_name,
            PromptSection.new(
                "narrator_verbosity",
                content,
                AttentionZone.Recency,
                SectionCategory.Guardrail,
            ),
        )

    def register_vocabulary_section(self, agent_name: str, vocabulary: str) -> None:
        """Inject narrator vocabulary/complexity instructions.

        Only applies to narrating agents. Non-narrating agents are silently skipped.
        Story 14-4: Per-session vocabulary control.

        vocabulary: one of 'accessible', 'literary', 'epic'
        """
        if agent_name not in _NARRATING_AGENTS:
            return

        _VOCAB_MAP: dict[str, str] = {
            "accessible": (
                "[NARRATION VOCABULARY]\n"
                "Use simple, direct language. Prefer common words over obscure "
                "ones. Keep sentences short and clear. Aim for approximately "
                "8th-grade reading level. No archaic constructions or elaborate "
                "metaphors."
            ),
            "epic": (
                "[NARRATION VOCABULARY]\n"
                "Use elevated, archaic, or mythic diction. Embrace elaborate "
                "sentence structures, rare words, and poetic constructions. "
                "Channel the cadence of sagas, epics, and high fantasy prose. "
                "Unrestricted complexity."
            ),
        }
        content = _VOCAB_MAP.get(
            vocabulary,
            (
                "[NARRATION VOCABULARY]\n"
                "Use rich but clear prose. Employ varied vocabulary and literary "
                "devices where they serve the narrative. Balance elegance with "
                "accessibility — vivid but not purple."
            ),
        )

        self.register_section(
            agent_name,
            PromptSection.new(
                "narrator_vocabulary",
                content,
                AttentionZone.Late,
                SectionCategory.Format,
            ),
        )

    def register_footnote_protocol_section(self, agent_name: str) -> None:
        """Inject the footnote protocol instruction into the narrator prompt.

        Story 9-11.
        """
        content = """\
[FOOTNOTE PROTOCOL]
When you reveal new information or reference something the party previously learned,
include a numbered marker in your prose like [1], [2], etc.

For each marker, emit a footnote in your structured output with:
- summary: one-sentence description of the fact
- category: one of Lore, Place, Person, Quest, Ability
- is_new: true if this is a new revelation, false if referencing prior knowledge
- fact_id: a short stable slug derived from the content (e.g. "grove-corruption",
  "courier-reads-lips", "marrow-name-on-wall"). Required for both new facts and
  callbacks — use the SAME slug when re-mentioning a previously-revealed fact
  so the journal de-dupes correctly. Lowercase, hyphen-separated, no spaces.

Example prose: "As you enter the grove, Reva feels a deep wrongness [1]."
Example footnote: { "marker": 1, "summary": "Corruption detected in the grove", "category": "Place", "is_new": true, "fact_id": "grove-corruption" }

If you reference something the party already knows, set is_new to false and reuse the same fact_id.
If nothing new is revealed and nothing prior is referenced, omit the footnotes array entirely."""

        self.register_section(
            agent_name,
            PromptSection.new(
                "footnote_protocol",
                content,
                AttentionZone.Late,
                SectionCategory.Format,
            ),
        )

    def register_resource_section(
        self,
        agent_name: str,
        declarations: list[dict],  # ResourceDeclaration-like dicts
        state: dict[str, float],
    ) -> None:
        """Inject genre resource state into the narrator prompt (story 16-1).

        Serializes current resource values into a human-readable block in the Valley zone.
        Empty declarations produce no section.

        Each declaration dict must have: name, label, starting, max, voluntary, decay_per_turn.
        """
        if not declarations:
            return

        lines = ["## GENRE RESOURCES — Current State"]
        for decl in declarations:
            current = state.get(decl["name"], decl["starting"])
            vol_label = "voluntary" if decl.get("voluntary", True) else "involuntary"
            line = f"{decl['label']}: {current}/{decl['max']} ({vol_label})"
            decay = decl.get("decay_per_turn", 0.0)
            if abs(decay) > 1e-9:
                line += f", decay {abs(decay)}/turn"
            lines.append(line)

        self.register_section(
            agent_name,
            PromptSection.new(
                "genre_resources",
                "\n".join(lines),
                AttentionZone.Valley,
                SectionCategory.State,
            ),
        )

    def register_npc_roster_section(
        self,
        agent_name: str,
        *,
        npc_pool: list[NpcPoolMember],
        npcs: list[Npc],
    ) -> None:
        """Inject canonical NPC identity data into the narrator prompt.

        Story 37-44 wired the original (registry-fed) projection. Wave 2A
        (story 45-47) splits the source into two stores while preserving
        the narrator-facing format — the gaslight discipline. The narrator
        sees one list of "people who exist in this world"; storage shape
        does not leak.

        Sources:
        - ``npc_pool`` — identity-only ``NpcPoolMember`` entries (regenerable
          cast pool; no last-seen, no mechanical state).
        - ``npcs`` — stateful ``Npc`` records. Identity fields plus
          ``last_seen_location`` line when set.

        Placed in the Early zone (not Valley): identity is acute data, not
        background lore — if it drifts to Valley the narrator attends to it
        less over long sessions, which is the exact drift the original
        Story 37-44 fix was for.
        """
        if not npc_pool and not npcs:
            return

        lines = ["## KNOWN NPCS — Canonical Identity (do not contradict)"]

        for member in npc_pool:
            parts: list[str] = [member.name]
            tags: list[str] = []
            if member.pronouns:
                tags.append(member.pronouns)
            if member.role:
                tags.append(member.role)
            if tags:
                parts.append(f"({', '.join(tags)})")
            if member.appearance:
                parts.append(f"— {member.appearance}")
            lines.append("- " + " ".join(parts))

        for npc in npcs:
            parts = [npc.core.name]
            tags = []
            if npc.pronouns:
                tags.append(npc.pronouns)
            if tags:
                parts.append(f"({', '.join(tags)})")
            if npc.appearance:
                parts.append(f"— {npc.appearance}")
            if npc.last_seen_location:
                parts.append(f"[last seen: {npc.last_seen_location}]")
            # Coarsened disposition stance — the "emotional perception is
            # POV" layer the closing instruction references. Story 50-12:
            # without this the narrator sees who exists but not how they
            # feel, forcing a per-NPC query_npc round-trip. The raw
            # disposition.value integer is world-state-agent-only and MUST
            # NOT reach this always-on narrator section (ADR-104/105
            # perception firewall) — emit only the qualitative band.
            parts.append(f"[attitude: {npc.disposition.attitude().value}]")
            lines.append("- " + " ".join(parts))

        lines.append(
            "Use these exact pronouns and roles. Physical identity is "
            "canonical; only emotional perception is POV."
        )

        self.register_section(
            agent_name,
            PromptSection.new(
                "npc_roster",
                "\n".join(lines),
                AttentionZone.Early,
                SectionCategory.State,
            ),
        )

    def register_region_section(
        self,
        agent_name: str,
        *,
        region_projection: object,
    ) -> None:
        """Inject the party's current dungeon region into the narrator prompt.

        Beneath Sünden BETTER fix (seam 1+2). ``region_projection`` is a
        ``sidequest.dungeon.region_projection.RegionProjection`` (typed
        ``object`` here to keep the prompt framework free of a
        ``sidequest.dungeon`` import — dungeon depends on game models).

        Gaslight discipline (the doctrine reference is
        ``world_materialization._apply_npc`` → ``snap.npcs`` →
        ``register_npc_roster_section``): the narrator sees ONE structured
        canonical-state block describing where the party is and the exact
        ways out — it never learns this is a re-projected procedural graph.
        The exit ids are the **constrained move vocabulary**: the narrator
        is told to put one of these EXACT ids in its ``current_region``
        patch, which is what makes the frontier look-ahead worker expand
        the real dungeon instead of the narrator inventing scene titles.

        Early zone (not Valley): location is acute, load-bearing state —
        the exact discipline the NPC roster fix established. Empty exits
        is valid (a dead-end region) and still renders so the narrator
        does not invent corridors out of a sealed chamber.
        """
        if region_projection is None:
            return

        rp = cast("RegionProjection", region_projection)
        lines = [
            "## YOU ARE HERE — Current Region (canonical; do not contradict)",
            f"Region: {rp.theme_display} [{rp.region_id}]",
        ]
        # Register is a procedural-theme field; cartography region-mode worlds
        # leave it blank. Omit the line rather than render a dangling "Register:".
        if rp.register:
            lines.append(f"Register: {rp.register}")
        lines.append(f"Flavor: {rp.flavor}")
        if rp.motifs:
            lines.append("Motifs: " + ", ".join(rp.motifs))
        if rp.depth_score is not None:
            lines.append(f"Depth: {rp.depth_score:.1f} (deeper = graver, more lethal in tone)")

        visible = [e for e in rp.exits if not e.hidden]
        hidden = [e for e in rp.exits if e.hidden]
        if visible:
            lines.append("Exits from this region (describe these as the way out):")
            for e in visible:
                tag = " (a shortcut back toward the surface)" if e.shortcut else ""
                lines.append(f"- {e.kind} → {e.to_region_id}{tag}")
        else:
            lines.append(
                "Exits from this region: none obvious — this is a dead end "
                "or sealed chamber. Do NOT invent a corridor out; the way "
                "forward is back the way the party came, or a discovery."
            )
        if hidden:
            lines.append(
                "Concealed ways (do NOT volunteer; only if the party "
                "actively searches and finds one):"
            )
            for e in hidden:
                lines.append(f"- {e.kind} → {e.to_region_id} [hidden]")

        example_id = (visible or hidden)[0].to_region_id if (visible or hidden) else rp.region_id
        lines.append(
            "MOVEMENT RULE: when the party leaves this region, set "
            "current_region in the game_patch to one of the EXACT region "
            f"ids listed above (e.g. {example_id}). Never invent a region "
            "id or a scene-title slug — only these ids advance the real map."
        )

        # Beneath Sünden is a LETHAL megadungeon, not a scenic walk
        # (genre truth: grave, Moria-as-tragedy, no winking). The curated
        # bestiary is already in this prompt (the Monster Manual section);
        # this directive is what makes the narrator USE it — without it the
        # narrator was running a quiet, empty delve. SOUL doctrine made
        # explicit at the load-bearing seam: Living World (the dark acts on
        # its own — it hunts, it does not wait for the party), Genre Truth
        # (lethal consequences), Cut the Dull Bits (a delve beat with no
        # complication is not a scene). Gaslight discipline: this is a GM
        # directive inside the canonical region block, the same register as
        # the MOVEMENT RULE above — the narrator never learns it is a
        # re-projected graph.
        #
        # Gated on is_dungeon: this is procedural-megadungeon genre truth.
        # Cartography region-mode worlds (e.g. tea_and_murder/glenross) reuse
        # the same YOU-ARE-HERE section + MOVEMENT RULE above but must NOT
        # inherit "the dark hunts you" — a cosy-mystery narrator told it is in
        # a lethal dungeon is its own Illusionism failure.
        if rp.is_dungeon:
            depth = rp.depth_score if rp.depth_score is not None else 0.0
            pressure = (
                "near the threshold — but Sünden is wrong even at its mouth "
                "(the Watcher waited at Moria's gate): the dark reaches UP for "
                "those who linger"
                if depth < 1.0
                else "deep — the press of stone and the things that own it is "
                "constant; survival, not scenery, is the question every beat"
            )
            lines.append(
                "THE DUNGEON IS ALIVE AND HOSTILE (act on this every turn): "
                f"the party is {pressure}. Draw on the Monster Manual already "
                "in this prompt — its wandering horrors, its apex dweller — and "
                "make the dark PUSH BACK: tracks then sound then the thing "
                "itself; pursuit, ambush from a side passage, a scavenger drawn "
                "by blood or light, the deep stirring at noise. Telegraph "
                "(Diamonds and Coal — bait the hook, then let it bite), escalate "
                "with depth, and never let a delve beat pass with no pressure, "
                "discovery, or cost. This is not a monster-mash: it is grave, "
                "earned, Moria-as-tragedy dread that PAYS OFF. If the last beats "
                "were quiet, the next must not be — something in Sünden has "
                "found them."
            )

        self.register_section(
            agent_name,
            PromptSection.new(
                "current_region",
                "\n".join(lines),
                AttentionZone.Early,
                SectionCategory.State,
            ),
        )

    def register_chassis_voice_section(
        self,
        agent_name: str,
        chassis_registry: dict[str, ChassisInstance],
        character_name: str,
    ) -> None:
        """Inject chassis-as-speaker voice data into the narrator prompt.

        The narrator already sees chassis names via the NPC roster section.
        This section adds the register, vocal tics, silence register, and
        current bond-tier address form so chassis dialogue stays consistent
        across turns. (Pre-Wave-2A the chassis-as-ship_ai projection lived
        in the legacy ``npc_registry``, dropped in story 45-52.)

        Empty registry or no-voice chassis produces no output (zero-byte
        leak discipline, mirrors npc_roster). Slice-scope: active character
        is identified via the bond_seed placeholder id "player_character" —
        full chargen rebind to real player_id is a follow-up.

        Placed in the Early zone alongside the NPC roster: chassis voice is
        acute identity data, not background lore.
        """
        from sidequest.agents.subsystems.chassis_voice import (
            resolve_chassis_name_form,
        )

        if not chassis_registry:
            return

        # Build the stub _CharacterLike per slice scope.
        first_name, last_name = _split_player_name(character_name)

        class _StubCharacter:
            def __init__(self) -> None:
                self.id = "player_character"
                self.first_name = first_name
                self.last_name = last_name
                self.nickname: str | None = None

        stub = _StubCharacter()

        rendered_lines: list[str] = []
        for chassis in chassis_registry.values():
            if chassis.voice is None:
                continue
            name_form = resolve_chassis_name_form(chassis, stub)
            tics = "; ".join(chassis.voice.vocal_tics) if chassis.voice.vocal_tics else "(none)"
            silence = chassis.voice.silence_register or "(unspecified)"
            rendered_lines.append(
                f"- {chassis.name} (chassis voice — {chassis.voice.default_register}): "
                f'addresses you as "{name_form}". Vocal tics: {tics}. '
                f"Silence reads as: {silence}."
            )

        if not rendered_lines:
            return

        body = "\n".join(
            [
                "## CHASSIS VOICES — Speak as them with this register",
                *rendered_lines,
                "Use the address-form exactly. The chassis's tone is set; "
                "the narrator's job is to honor it.",
            ]
        )

        self.register_section(
            agent_name,
            PromptSection.new(
                "chassis_voices",
                body,
                AttentionZone.Early,
                SectionCategory.State,
            ),
        )

    def register_chassis_position_section(
        self,
        agent_name: str,
        positions: dict[str, str | None],
    ) -> None:
        """Inject per-PC chassis-interior position into the narrator prompt.

        The Ship-tab map mirrors ``character.current_room``; the narrator is
        the source of truth, so the prompt must (a) tell the narrator where
        each PC currently is, and (b) instruct the narrator to emit a
        ``state_patch`` updating ``current_room`` whenever the prose moves a
        character between rooms.

        ``positions`` is ``{character_name: current_room | None}``. Entries
        with ``None`` are filtered (PC has no chassis position yet — usually
        means they haven't boarded). Empty filtered dict produces no section
        (zero-byte-leak discipline).
        """
        live = {name: room for name, room in positions.items() if room}
        if not live:
            return

        lines = ["## CREW POSITIONS — chassis interior (narrator-tracked)"]
        for name, room in live.items():
            lines.append(f"- {name} is in the {room}.")
        lines.append(
            "When your narration moves a character to a different room, emit "
            "a ``state_patch`` updating ``/characters/<name>/current_room`` so "
            "the Ship tab stays in sync. The map cannot move people on its own."
        )

        self.register_section(
            agent_name,
            PromptSection.new(
                "chassis_positions",
                "\n".join(lines),
                AttentionZone.Early,
                SectionCategory.State,
            ),
        )

    def register_party_peer_section(
        self,
        agent_name: str,
        party_peers: list[PartyPeer],
    ) -> None:
        """Inject canonical party-peer identity data into the narrator prompt.

        Story 37-36 (port-drift reopen): in sealed-letter multiplayer, the
        acting player's narrator turn must see canonical identity for other
        PCs — otherwise pronouns/race/class drift across saves (playtest 3:
        Blutka he/him became she/her in Orin's save). Parallels
        ``register_npc_roster_section`` for PCs instead of NPCs.

        Physical identity is canonical; perception (mood, tactics, feelings)
        stays POV and is not rendered here. Empty list produces no section
        (zero-byte leak discipline — solo sessions pay nothing). Placed in
        the Early zone next to the NPC roster: identity is acute data, not
        background lore.
        """
        if not party_peers:
            return

        lines = ["## PARTY MEMBERS — Canonical Identity (do not contradict)"]
        for peer in party_peers:
            tags: list[str] = []
            if peer.pronouns:
                tags.append(peer.pronouns)
            tags.append(f"{peer.race} {peer.char_class}")
            tags.append(f"level {peer.level}")
            lines.append(f"- {peer.name} ({', '.join(tags)})")
        lines.append(
            "Use these exact pronouns, race, and class for every party "
            "member. Physical identity is canonical; only emotional "
            "perception is POV."
        )

        self.register_section(
            agent_name,
            PromptSection.new(
                "party_peer_roster",
                "\n".join(lines),
                AttentionZone.Early,
                SectionCategory.State,
            ),
        )
