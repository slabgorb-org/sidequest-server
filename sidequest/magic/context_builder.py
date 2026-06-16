"""Builds the magic-context block injected into narrator pre-prompt.

When a world has magic.yaml loaded (snapshot.magic_state is not None),
the block is emitted alongside other pre-prompt scaffolding. When
absent, returns empty string and narrator pre-prompt is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sidequest.magic.reliquary_ops import DEFAULT_DIVINE_FAVOR_THRESHOLD
from sidequest.magic.state import BarKey, MagicState

if TYPE_CHECKING:
    # Guarded to break the circular import:
    # sidequest.magic.__init__ → context_builder → genre.models.items →
    # genre.__init__ → genre.archetype → ... → game → telemetry.spans →
    # back to sidequest.magic during its still-loading state.
    from sidequest.genre.models.items import WorldItem


def build_magic_context_block(
    *,
    magic_state: MagicState | None,
    actor_id: str | None,
    reliquaries: list[WorldItem] | None = None,
) -> str:
    """Return the pre-prompt magic-context block (or empty string if state absent).

    ``reliquaries`` is the world's item-catalog reliquaries section
    (``World.items.reliquaries``). When the actor holds a ``divine_favor``
    bar at or above the reliquary threshold and the session's free use
    hasn't yet been spent, the eligible reliquary effect texts are
    rendered into an ``<available-reliquaries>`` block so the narrator
    can ground the Cleric's options without the daemon hallucinating a
    relic that doesn't exist.
    """
    if magic_state is None:
        return ""

    config = magic_state.config
    lines: list[str] = ["ACTIVE MAGIC CONTEXT — " + config.world_slug]
    lines.append(f"allowed_sources: {config.allowed_sources}")
    lines.append(f"active_plugins: {config.active_plugins}")
    # Canonical cost-type vocabulary. The engine routes
    # ``magic_working.costs[<cost_type>]`` directly to a ledger bar of
    # the same id; an off-vocabulary cost (improvised from genre
    # folklore — `slots`, `mp`, `mana`) is silently dropped with a
    # warning, exactly the SOUL.md illusionism failure the GM panel
    # exists to catch (playtest 2026-05-08, caverns_sunden Mage).
    lines.append(f"valid_cost_types: {config.cost_types}")

    hard_limit_ids = [h.id for h in config.hard_limits]
    lines.append(f"hard_limits: {hard_limit_ids}")

    wk = config.world_knowledge
    wk_str = wk.primary
    if wk.local_register:
        wk_str = f"{wk.primary} (local register: {wk.local_register})"
    lines.append(f"world_knowledge: {wk_str}")

    if actor_id is not None:
        lines.append(f"active_ledger_for_{actor_id}:")
        for spec in config.ledger_bars:
            if spec.scope == "character":
                key = BarKey(scope="character", owner_id=actor_id, bar_id=spec.id)
                try:
                    bar = magic_state.get_bar(key)
                except KeyError:
                    continue
                threshold_str = ""
                if spec.direction == "down" and spec.threshold_low is not None:
                    threshold_str = (
                        f" (threshold_low: {spec.threshold_low:.2f} → "
                        f"{spec.consequence_on_low_cross or '...'})"
                    )
                elif spec.direction == "up" and spec.threshold_high is not None:
                    threshold_str = (
                        f" (threshold_high: {spec.threshold_high:.2f} → "
                        f"{spec.consequence_on_high_cross or '...'})"
                    )
                lines.append(f"  {spec.id}: {bar.value:.2f}{threshold_str}")

    # World-scope bars (e.g. hegemony_heat)
    for spec in config.ledger_bars:
        if spec.scope == "world":
            key = BarKey(scope="world", owner_id=config.world_slug, bar_id=spec.id)
            try:
                bar = magic_state.get_bar(key)
            except KeyError:
                continue
            lines.append(f"  {spec.id} (world): {bar.value:.2f}")

    lines.append("")
    lines.append(
        "If your narration depicts a magic working, emit a magic_working field "
        "in your game_patch with required fields for the firing plugin. The "
        "validator enforces hard_limits; describing a working that violates one "
        "will surface a DEEP_RED flag in the GM panel. Cost keys in "
        "magic_working.costs MUST be drawn from valid_cost_types above and "
        "MUST exactly match an entry of active_ledger_for_<actor> when "
        "debiting a character bar — do NOT improvise cost names from genre "
        "folklore (B/X 'slots', JRPG 'mp', etc.). Off-vocabulary costs are "
        "dropped silently with a warning."
    )

    # Story 47-10 — learned-magic block. When the actor has prepared
    # spells, surface known/prepared/slot info to the narrator. The
    # narrator is bound by ADR-009 to only name spells in the
    # <prepared> list (don't narrate unlisted actions).
    if actor_id is not None:
        prepared = magic_state.prepared_spells.get(actor_id, {})
        known = magic_state.known_spells.get(actor_id, [])
        if prepared and any(spells for spells in prepared.values()):
            lines.append("")
            lines.append(f'<learned-magic actor="{actor_id}">')
            if known:
                lines.append(f"  <known>{', '.join(known)}</known>")
            lines.append("  <prepared>")
            for level in sorted(prepared.keys()):
                spell_ids = prepared[level]
                if spell_ids:
                    lines.append(f"    <l{level}>{', '.join(spell_ids)}</l{level}>")
            lines.append("  </prepared>")
            # Slot remaining counts per level — read from the ledger.
            # Bar id convention: slots_l<N> (per seed_learned_v1_state).
            slot_lines: list[str] = []
            for level in sorted(prepared.keys()):
                bar_id = f"slots_l{level}"
                key = BarKey(scope="character", owner_id=actor_id, bar_id=bar_id)
                try:
                    bar = magic_state.get_bar(key)
                except KeyError:
                    continue
                # Read max from the bar's spec range.
                max_slots = bar.spec.range[1] if bar.spec.range else "?"
                slot_lines.append(
                    f"    <l{level}>{bar.value:.0f}/{max_slots:.0f} remaining</l{level}>"
                )
            if slot_lines:
                lines.append("  <slots>")
                lines.extend(slot_lines)
                lines.append("  </slots>")
            lines.append("</learned-magic>")
            lines.append(
                "Per ADR-009: cast_spell narration MUST name a spell from the "
                "<prepared> list above. Unprepared spells are not available to "
                "the actor this turn — do not narrate them as cast."
            )

    # Cleric reliquary register (Story 47-5). Surfaced only when:
    #   1. the actor has a `divine_favor` ledger bar (Cleric class),
    #   2. that bar is at or above the reliquary threshold (0.7), and
    #   3. the session's once-per-session free use has not been spent.
    # The narrator reads the verbatim divine_favor_effect text to ground
    # the Cleric's invocation prose — see reliquary_ops.invoke_reliquary
    # for the mechanical gate that runs when the player commits.
    if actor_id is not None and reliquaries:
        favor_key = BarKey(scope="character", owner_id=actor_id, bar_id="divine_favor")
        try:
            favor_bar = magic_state.get_bar(favor_key)
        except KeyError:
            favor_bar = None
        free_use_spent = actor_id in magic_state.reliquary_free_use_spent
        eligible = [r for r in reliquaries if r.model_dump().get("divine_favor_effect")]
        if (
            favor_bar is not None
            and favor_bar.value >= DEFAULT_DIVINE_FAVOR_THRESHOLD
            and not free_use_spent
            and eligible
        ):
            lines.append("")
            lines.append(
                f'<available-reliquaries actor="{actor_id}" divine_favor="{favor_bar.value:.2f}">'
            )
            lines.append(
                f"  The Cleric holds reliquaries that, at "
                f"divine_favor >= {DEFAULT_DIVINE_FAVOR_THRESHOLD:.1f}, "
                "may be invoked for one free narrative effect this session. "
                "Spending the effect closes the option for the remainder of "
                "the session — describe the invocation only when the player "
                "explicitly commits to it."
            )
            for r in eligible:
                dump = r.model_dump()
                effect = str(dump.get("divine_favor_effect", "")).strip()
                # Render multi-line effect text as a yaml-style block so the
                # narrator sees a clean shape without our indentation
                # bleeding into the prose.
                indented = "\n".join("    " + ln for ln in effect.splitlines() if ln)
                lines.append(f"  - id: {r.id}")
                lines.append(f"    name: {r.name}")
                lines.append("    effect: |")
                lines.append(indented)
            lines.append("</available-reliquaries>")

    if "innate_v1" in config.active_plugins:
        lines.append("")
        lines.append("Example innate_v1 working — reflexive surfacing under stress:")
        lines.append(
            "  When a PC faces immediate stress (an uncanny presence, a sudden "
            "alien stimulus, an alien register pressing in from outside), innate "
            "flavor may surface involuntarily. Narrate the triggering stimulus "
            "and any immediate reflex follow-through (a flinch, a recoil, a "
            "tightening grip) — but do NOT narrate what the PC perceives, "
            "thinks, names, or feels about the experience. The cost lands on "
            "the actor's sanity bar. The flavor is one of the chargen-bound "
            "options (acquired, born_to_it, trained_register, covenant_lineage) "
            "and the consent_state is involuntary for stress-triggered "
            "surfacing. The magic_working JSON shape:"
        )
        lines.append(
            '    {"plugin": "innate_v1", "mechanism": "condition", '
            '"actor": "<character_name>", "costs": {"sanity": 0.15}, '
            '"domain": "psychic", "narrator_basis": "reflexive recoil from '
            'uncanny presence", '
            '"flavor": "<character\'s chargen-bound flavor>", '
            '"consent_state": "involuntary"}'
        )

    return "\n".join(lines)
