"""Mutation context block for the narrator pre-prompt (Plan 2 §5.4, story 102-7).

The ``sidequest/magic/context_builder.py`` sibling: a narrator that cannot
SEE a PC's mutation surface cannot call ``use_mutation``, honor usage
limits, or narrate the real powers — it improvises. Same static/volatile
split as the magic block (ADR-009/112 prompt-zone discipline):

- **static** — session-stable: each seeded character's owned mutations
  (positives with effect/cost/usage, negatives with their burdens) and the
  catalog's narrator register. Changes only on acquisition.
- **volatile** — per-turn: MP remaining and usage counters.

Both return ``""`` when there is no mutation surface — a world without
mutations pays zero prompt tokens (the 61-12 single-chokepoint economics).
"""

from __future__ import annotations

from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.state import CharacterMutationState, MutationState


def _positive_line(catalog: MutationCatalog, mutation_id: str) -> str:
    try:
        m = catalog.positive_by_id(mutation_id)
    except KeyError:
        # State references an id the catalog no longer carries (content
        # drift) — surface it honestly rather than dropping the row.
        return f"- {mutation_id} (NOT IN CATALOG — content drift; do not narrate this power)"
    bits = [f"- {m.name} ({m.id}): {m.effect}"]
    if m.strain_cost > 0:
        bits.append(f"costs {m.strain_cost} System Strain")
    if m.usage != "at_will":
        bits.append(f"{m.usage} x{m.uses_per_period}")
    if m.save is not None and m.save.stat:
        bits.append(f"target saves {m.save.stat} ({m.save.effect})")
    return "; ".join(bits)


def _negative_line(catalog: MutationCatalog, mutation_id: str) -> str:
    for n in catalog.negatives:
        if n.id == mutation_id:
            return f"- {n.name} ({n.id}): {n.effect}"
    return f"- {mutation_id} (NOT IN CATALOG — content drift; do not narrate this burden)"


def build_mutation_static_block(
    *,
    mutation_state: MutationState | None,
    catalog: MutationCatalog | None,
) -> str:
    """Session-static mutation surface (or empty string when absent)."""
    if mutation_state is None or catalog is None or not mutation_state.characters:
        return ""
    lines: list[str] = ["ACTIVE MUTATION CONTEXT (the mechanical truth — never invent powers):"]
    if catalog.narrator_register:
        lines.append(f"register: {catalog.narrator_register}")
    for actor, cs in mutation_state.characters.items():
        lines.append(f"{actor} carries:")
        for mid in cs.positive_ids:
            lines.append(_positive_line(catalog, mid))
        for mid in cs.negative_ids:
            lines.append(_negative_line(catalog, mid))
        if not cs.positive_ids and not cs.negative_ids:
            lines.append("- no expressed mutations (MP banked)")
    lines.append(
        "Resolve every mutation use mechanically (the use_mutation tool / the "
        "mutation beat) — costs and limits are real; a refused use means the "
        "body does not answer."
    )
    return "\n".join(lines)


def _usage_line(cs: CharacterMutationState, mutation_id: str) -> str | None:
    counter = cs.usage.get(mutation_id)
    if counter is None:
        return None
    return f"  {mutation_id}: {counter.used} used this {counter.period.removeprefix('per_')}"


def build_mutation_volatile_block(
    *,
    mutation_state: MutationState | None,
    catalog: MutationCatalog | None,
) -> str:
    """Per-turn mutation ledger: MP + usage counters (or empty string)."""
    if mutation_state is None or catalog is None or not mutation_state.characters:
        return ""
    lines: list[str] = ["MUTATION LEDGER (live values):"]
    for actor, cs in mutation_state.characters.items():
        lines.append(f"{actor}: {cs.mp_remaining} MP remaining")
        for mid in cs.positive_ids:
            usage = _usage_line(cs, mid)
            if usage is not None:
                lines.append(usage)
    return "\n".join(lines)
