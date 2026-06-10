"""Story 102-7 RED — BeatSelection carries ``mutation_id`` (the spell_id mirror).

THE GAP (AWN Plan 2 spec §6.3, epic 102 "live wiring"): the narrator-driven
apply_beat path routes a WN ``cast_spell`` beat to the cast spine because
``BeatSelection.spell_id`` (story 47-10, hardened by 102-2) names WHICH spell.
AWN's marquee mechanic has no equivalent: when the narrator applies a
mutation-resolution beat (mutant_wasteland's ``mutant_ability``), there is no
sidecar field naming WHICH mutation — so the apply path cannot reach
``sidequest.mutation.use_ops.use_mutation`` and the beat stays bare narration.

THE CONTRACT THESE TESTS PIN: ``BeatSelection`` gains ``mutation_id``,
mirroring ``spell_id`` exactly — ``None`` default, parsed by ``from_dict``,
absent input tolerated. The apply-path behavior that consumes it lives in
tests/server/test_102_7_mutation_beat_use_ops.py.

CLAUDE.md rule coverage: this is a protocol-contract test (the dataclass IS
the narrator tool contract); behavior/wiring proof is span-shaped in the
sibling file, never source-text.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import BeatSelection


def test_beat_selection_accepts_mutation_id_kwarg() -> None:
    """The sidecar field exists and round-trips through the constructor."""
    sel = BeatSelection(
        actor="Rux",
        beat_id="mutant_ability",
        mutation_id="exotic/acid_spit",
    )
    assert sel.mutation_id == "exotic/acid_spit"


def test_beat_selection_mutation_id_defaults_none() -> None:
    """Non-mutation beats are untouched: the field defaults to None (the
    spell_id precedent — legacy callers never pass it)."""
    sel = BeatSelection(actor="Rux", beat_id="shoot")
    assert sel.mutation_id is None


def test_from_dict_parses_mutation_id() -> None:
    """The narrator emits beat selections as JSON dicts; from_dict must lift
    mutation_id the same way it lifts spell_id."""
    sel = BeatSelection.from_dict(
        {
            "actor": "Rux",
            "beat_id": "mutant_ability",
            "outcome": "Success",
            "mutation_id": "exotic/acid_spit",
        }
    )
    assert sel.mutation_id == "exotic/acid_spit"


def test_from_dict_tolerates_absent_mutation_id() -> None:
    """Legacy narrator payloads (no mutation_id key) must keep parsing —
    regression guard for every non-AWN genre."""
    sel = BeatSelection.from_dict({"actor": "Rux", "beat_id": "shoot", "outcome": "Success"})
    assert sel.mutation_id is None


def test_from_dict_spell_id_unaffected() -> None:
    """The new field must not perturb the existing cast sidecar (102-2)."""
    sel = BeatSelection.from_dict(
        {
            "actor": "Vesska",
            "beat_id": "cast_spell",
            "outcome": "Success",
            "spell_id": "foundation_of_flame",
        }
    )
    assert sel.spell_id == "foundation_of_flame"
    assert sel.mutation_id is None
