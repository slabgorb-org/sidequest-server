"""Story 59-1 RED — SDK confrontation ENGAGEMENT tool/prompt path.

See sprint/context/context-story-59-1.md (Architecture Decision, Houlihan
2026-05-22). The bug is NOT "narrator never calls advance_confrontation".
Engagement = the narrator setting the structured ``confrontation`` field
(orchestrator.py:304), consumed by the server at narration_apply.py:2531 to
create the StructuredEncounter. ``advance_confrontation`` only ADVANCES an
already-active encounter and errors if none is active.

On the default ``anthropic_sdk`` backend (ADR-101/102) the narrator gets the
full registry (orchestrator.py:3232/3322), so "registered == offered". The
real gap is that NO offered tool can WRITE ``result.confrontation``:
  - ``apply_world_patch`` carries the sibling game_patch fields but not this one
  - ``advance_confrontation`` can't start (and its description is combat-only)
  - ``generate_encounter`` — where ADR-111/57-4 stranded the social trigger
    criteria — is a stub that always returns a fatal error.

These tests pin the corrected ACs (2, 3, 4). They FAIL today by design.

NOTE on impl-agnosticism: the Architect's reuse-first recommendation is to
extend ``apply_world_patch`` with a ``confrontation`` field. A dedicated
``begin_confrontation`` tool is the documented fallback. The AC2 test accepts
either; AC3/AC4 are written against the recommended path — if Dev deviates,
update these + log a Design Deviation.
"""

from __future__ import annotations

from pathlib import Path

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import default_registry

# Social confrontation types tea_and_murder offers (rules.yaml). These are the
# types that must be reachable through the SDK engagement path.
_SOCIAL_TYPES = ("negotiation", "social_duel", "trial", "auction", "scandal")

# Property names a tool might expose to set the engagement TYPE. advance_confrontation's
# ``confrontation_id`` is explicitly NOT one of these (it's a forward-compat id, not a
# type that STARTS an encounter).
_ENGAGEMENT_FIELD_NAMES = ("confrontation", "confrontation_type")


def _defs_by_name() -> dict[str, object]:
    return {d.name: d for d in default_registry.tool_definitions()}


# ---------------------------------------------------------------------------
# AC2 — an SDK tool must be able to WRITE result.confrontation
# ---------------------------------------------------------------------------


def test_some_sdk_tool_can_write_the_confrontation_engagement_field() -> None:
    """AC2: There must be an offered SDK tool (other than advance_confrontation)
    whose input schema lets the narrator set the confrontation engagement type.

    FAILS today: only advance_confrontation carries a confrontation-ish arg
    (confrontation_id, a forward-compat id — NOT a starting type), and it is
    explicitly excluded. apply_world_patch carries no such field.
    """
    defs = _defs_by_name()
    writers = []
    for name, d in defs.items():
        if name == "advance_confrontation":
            continue
        props = d.input_schema.get("properties", {})  # type: ignore[attr-defined]
        if any(field in props for field in _ENGAGEMENT_FIELD_NAMES):
            writers.append(name)
    assert writers, (
        "No offered SDK tool exposes a confrontation engagement field "
        f"({_ENGAGEMENT_FIELD_NAMES}). The narrator cannot set result.confrontation, "
        "so social confrontations never engage. Recommended fix: add a "
        "`confrontation` field to apply_world_patch. Offered tools: "
        f"{sorted(defs)}"
    )


def test_advance_confrontation_is_not_the_engagement_writer() -> None:
    """AC2 (negative): advance_confrontation must NOT be treated as the way to
    START a confrontation — it advances an active encounter's dial and its args
    are axis/delta, not a starting type. This guards against a 'fix' that just
    points engagement back at the wrong tool.
    """
    d = _defs_by_name()["advance_confrontation"]
    props = d.input_schema.get("properties", {})  # type: ignore[attr-defined]
    assert "axis" in props and "delta" in props, (
        "advance_confrontation should remain the advance-an-active-encounter tool "
        f"(axis/delta). Got props: {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# AC3 — social trigger criteria must live on a LIVE engagement tool, not the
# always-erroring generate_encounter stub.
# ---------------------------------------------------------------------------


def test_live_engagement_tool_description_carries_social_triggers() -> None:
    """AC3: The social trigger criteria must ride on the LIVE engagement tool's
    description. SDK selection keys on the tool description (ADR-111).

    Dev deviated from the Architect's reuse-first recommendation (extend
    apply_world_patch): the engagement writer is the dedicated
    ``begin_confrontation`` tool instead, because apply_world_patch is a
    deprecation-targeted escape hatch (ADR-011, target = zero spans) and
    bolting a load-bearing engagement path onto it is the wrong home. See the
    59-1 session-file Design Deviation. The contract is unchanged: the social
    trigger criteria ride on whichever LIVE tool the narrator calls to START a
    confrontation.
    """
    d = _defs_by_name()["begin_confrontation"]
    desc = d.description.lower()  # type: ignore[attr-defined]
    missing = [t for t in _SOCIAL_TYPES if t not in desc]
    assert not missing, (
        "begin_confrontation (the engagement writer) description is missing "
        f"social trigger types {missing}. The narrator reads tool descriptions "
        "to decide engagement (ADR-111); the social criteria must ride on the "
        "live start-confrontation tool, not a dead stub."
    )


def test_generate_encounter_cannot_be_the_engagement_path() -> None:
    """AC3 (negative): generate_encounter is a stub that always returns a fatal
    error, so engagement must NOT route through it. Story 59-1 relocated the
    social trigger criteria OFF this stub onto begin_confrontation (the dead
    stub mis-routed the SDK narrator — that was the engagement-regression root
    cause). Assert structurally that engagement does not depend on it:
    generate_encounter exposes no confrontation-engagement field, so it is not
    among AC2's engagement writers.
    """
    d = _defs_by_name()["generate_encounter"]
    props = d.input_schema.get("properties", {})  # type: ignore[attr-defined]
    assert not any(field in props for field in _ENGAGEMENT_FIELD_NAMES), (
        "generate_encounter must NOT expose a confrontation engagement field "
        f"({_ENGAGEMENT_FIELD_NAMES}) — it is an always-erroring stub that "
        "cannot create an encounter. Engagement routes through "
        f"begin_confrontation. generate_encounter props: {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# AC4 — the SDK prompt must not route STARTING through advance_confrontation.
# ---------------------------------------------------------------------------

_SDK_PROMPT = (
    Path(__file__).resolve().parents[1].parent
    / "sidequest"
    / "agents"
    / "narrator_prompts"
    / "output_only_sdk.md"
)


def test_sdk_prompt_does_not_route_starting_through_advance_confrontation() -> None:
    """AC4: output_only_sdk.md section 4 must not instruct the narrator to START
    a confrontation by calling advance_confrontation (which errors with no active
    encounter). advance_confrontation is for ADVANCING an active encounter.

    FAILS today: section 4 reads "STARTING / ADVANCING A CONFRONTATION OR
    ENCOUNTER ... call advance_confrontation (when ANY structured encounter
    BEGINS this turn ...)".
    """
    # Normalize whitespace so markdown line-wrapping doesn't hide the phrase
    # (the source wraps "BEGINS this\n   turn" across lines).
    normalized = " ".join(_SDK_PROMPT.read_text(encoding="utf-8").split())
    assert "advance_confrontation` (when ANY structured encounter BEGINS this turn" not in normalized, (
        "output_only_sdk.md still tells the narrator to call advance_confrontation "
        "when an encounter BEGINS — but that tool cannot start one. STARTING must "
        "route to the engagement-field writer; advance_confrontation is advance-only."
    )
