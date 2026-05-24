"""SDK-path cutover tests for Story 59-4 (ADR-113).

These tests pin the orchestrator-side state of the cutover:

  AC4 part 1: ``_SDK_TOOL_OWNED_FIELDS`` does NOT contain ``confrontation``.
    This is a regression guard. The field was intentionally absent before
    59-4 (per the explicit comment at orchestrator.py:1100-1110, since the
    SDK tool's store-write was clobbered by room.save). The cutover must
    not accidentally add it under the impression that the router's
    dispatch counts as "tool-owned". The router is a producer; the
    handler is the engager; neither rides on the SDK assembler's
    tool-owned partition.

  AC4 part 2: Sibling fields in ``_SDK_TOOL_OWNED_FIELDS`` remain
    unchanged. Regression guard against scope creep — a sloppy cutover
    that "tidied up" the dict would silently break sibling subsystems
    (magic_working, status_changes, ...) whose cutovers are deferred to
    later stories (59-5, etc.).

  AC4 part 3 (deferred, see Design Deviation #4): The
    ``_assemble_turn_result_sdk`` begin_confrontation lift at
    ``orchestrator.py:3321-3346`` is removed. This is *transitively
    enforced* by AC3 (no ``begin_confrontation`` tool in registry → the
    narrator cannot call it → the lift's iteration over
    ``result.tool_calls`` for ``begin_confrontation`` cannot fire). TEA
    does not write a separate fixture-construction test for the lift
    because ``_assemble_turn_result_sdk`` (orchestrator.py:3259) takes
    six heavily-coupled fixture inputs (extraction, raw_response,
    context, elapsed_ms, prompt_text, token counts) — the cost of
    constructing them isolates poorly and the lift is dead code after
    AC3 anyway. Reviewer (Granny) verifies removal by reading the diff.

Project rule coverage:
- "No Source-Text Wiring Tests" — AC4 uses dict reflection
  (``"confrontation" in _SDK_TOOL_OWNED_FIELDS``), not source greps.
- Memory ``feedback_one_mechanism_per_problem`` — sibling fields
  remaining proves the dict's structure is preserved, not "cleaned up".

These tests assert the post-cutover steady state. AC4 part 1 PASSES TODAY
(field is already absent) — that's intentional. It catches regression
during the cutover where someone misreads the AC text and adds the field
to the dict.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# AC4 part 1: confrontation absent from _SDK_TOOL_OWNED_FIELDS
# ---------------------------------------------------------------------------


def test_sdk_tool_owned_fields_does_not_contain_confrontation() -> None:
    """Regression guard for AC4 part 1. The cutover MUST NOT add
    ``confrontation`` to the SDK tool-owned partition under the
    impression that the router's dispatch counts as a tool-owned
    field. The router engages the engine directly on the canonical
    snapshot; the SDK assembler's tool-owned-zeroing logic is for
    fields a narrator-emitted tool call would otherwise double-apply.
    Confrontation is router-driven post-cutover and has no narrator
    tool to own it.

    PASSES TODAY by accident (the field was never in the dict for
    different reasons — see the comment block at
    orchestrator.py:1100-1110). The test stays as a regression guard
    against a "tidy the comment, accidentally add the field" cutover
    mistake.
    """
    from sidequest.agents.orchestrator import _SDK_TOOL_OWNED_FIELDS

    assert "confrontation" not in _SDK_TOOL_OWNED_FIELDS, (
        "Story 59-4 must not add 'confrontation' to _SDK_TOOL_OWNED_FIELDS. "
        "The router engages the engine directly on the canonical snapshot; "
        "there is no narrator tool whose write would double-apply. Adding "
        "the key would crash the import-time invariant at "
        f"orchestrator.py:3358-3367. Current dict keys: {sorted(_SDK_TOOL_OWNED_FIELDS)}"
    )


def test_sdk_tool_owned_fields_sibling_keys_remain_unchanged() -> None:
    """AC4 part 2: sibling keys must stay exactly as they were pre-cutover.
    A sloppy cutover that "tidied up" the dict would silently break
    sibling subsystems whose cutovers are still ahead of us
    (magic_working in 59-5, scenario_clue in 59-6, the three additive
    subsystems in 59-7). Pin the full key set.

    PASSES TODAY for the same reason as part 1 — but stays as scope-creep
    guard. If a Dev refactors `_SDK_TOOL_OWNED_FIELDS` during 59-4 to
    "clean up while we're here", this fails loudly.
    """
    from sidequest.agents.orchestrator import _SDK_TOOL_OWNED_FIELDS

    expected_keys = {
        "status_changes",
        "location",
        "magic_working",
        "beat_selections",
        "days_advanced",
        "affinity_progress",
        "game_patch_dict",
    }

    actual_keys = set(_SDK_TOOL_OWNED_FIELDS.keys())
    missing = expected_keys - actual_keys
    extra = actual_keys - expected_keys

    assert not missing, (
        f"sibling keys missing from _SDK_TOOL_OWNED_FIELDS: {sorted(missing)}. "
        "Story 59-4 must not remove sibling tool-owned fields — those cutovers "
        "are deferred to 59-5/6/7."
    )
    assert not extra, (
        f"unexpected keys in _SDK_TOOL_OWNED_FIELDS: {sorted(extra)}. "
        "If you intentionally added a tool-owned field, update this test "
        "AND add a session-file Design Deviation explaining the scope addition."
    )
