"""Story 61-18 — Audit CONFRONTATION_TRIGGER_CONSTRAINT dead-prose on the SDK path.

Ground truth (established in the 61-18 audit, RED phase):

``CONFRONTATION_TRIGGER_CONSTRAINT`` (``narrator_guardrails.py``) is the prose
that steers the model to fire a ``confrontation`` whenever the fiction
describes a stake-binding engagement. It exists for a real, recurring failure
(Pingpong 2026-05-03: the narrator wrote a textbook chase-firing beat but the
patch carried ``confrontation=None``).

On the default production backend (Anthropic SDK, ADR-101) the four Recency
guardrails are suppressed in ``build_narrator_prompt`` behind
``_maybe_register_legacy_guardrail`` (legacy-only). Story 57-4 / ADR-111
migrated the OTHER three guardrails to live SDK surfaces and pinned each in
``test_57_4_recency_guardrails_migration.py``:

  - ``npc_intro_visual_constraint`` → ``NARRATOR_OUTPUT_ONLY`` sidecar
  - ``npc_extraction_constraint``  → ``NARRATOR_OUTPUT_ONLY`` sidecar
  - ``location_patch_constraint``  → ``apply_world_patch`` tool description

The confrontation guardrail is the ONLY one of the four with NO migration
target and NO surviving migration test: ``test_57_4`` line 337-340 removed it
with the comment "the begin_confrontation tool was retired in Story 59-4
(ADR-113); confrontation engagement is now router-driven. The guardrail
migration target no longer exists." Nobody re-verified that the router
actually carries the trigger-recognition steering the prose used to carry.

Empirical finding (this audit): the load-bearing trigger-recognition prose
reaches the model through ZERO live SDK surfaces — not the IntentRouter
``_SYSTEM_PROMPT``, not any registered tool ``description``, not
``NARRATOR_OUTPUT_ONLY``. The router *names* ``confrontation`` as a subsystem
and gives generic category guidance ("a parley → a social-category type") but
none of the concrete beat→fire steering that ADR-111 §Alternatives B
explicitly preserved as the regression fingerprints ("Do NOT defer it to the
next turn", "the asking IS the trigger", social-pack triggers are binding).
The dead prose's *value* was lost, not migrated.

AC mapping:
  AC-1 (ground truth) — pinned by the steering-presence tests below: today the
        fingerprints reach no live SDK surface (the tests are RED).
  AC-2 (owner resolved with a test) — see the deviation in
        ``## Design Deviations``: a literal "fire a real turn through the SDK
        path" needs a live Haiku/SDK call (the router's classification judgment
        IS the thing under audit) and cannot run deterministically in CI. The
        faithful deterministic regression guard that "fails if confrontation
        triggering regresses to dead-prose" is the steering-presence test
        (dead-prose ⟺ steering absent). The engager-behavior test below proves
        the owning subsystem ACTUALLY engages on a dispatch (the AC-2 edge
        case: not merely registered).
  AC-3 (docstring/ADR reconciled) — Dev deliverable; the stale
        ``generate_encounter`` description is recorded as a delivery finding.

Project-rule coverage:
  - "No Source-Text Wiring Tests" (server CLAUDE.md): the steering-presence
    checks assert membership in the runtime prompt/description STRING VALUES
    (the actual artifacts sent to the model), not ``read_text()`` of a .py
    source file. This is the accepted prompt-vocabulary pattern — same shape as
    ``test_57_4``'s ``test_apply_world_patch_tool_description_carries_location_guardrail``
    and ``test_confidence_gate``'s ``test_router_prompt_instructs_per_dispatch_confidence``.
  - "No Silent Fallbacks": the engager test asserts a real encounter lands on
    the snapshot — no quiet no-op.
  - "Every Test Suite Needs a Wiring Test": the engager test drives the real
    ``run_dispatch_bank`` → registered ``run_confrontation_dispatch`` →
    ``snapshot.encounter`` path end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

# Importing the tools package wires all adapters onto default_registry so
# default_registry.tool_definitions() reflects production reality.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.dispatch_engagement_watcher import (
    detect_dispatch_engagement_mismatch,
)
from sidequest.agents.intent_router import _SYSTEM_PROMPT
from sidequest.agents.narrator_guardrails import CONFRONTATION_TRIGGER_CONSTRAINT
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY
from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.agents.tool_registry import default_registry
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Load-bearing regression fingerprints (sourced from the single-source-of-truth
# constant per ADR-111 §Implementation Notes). A faithful migration that
# references CONFRONTATION_TRIGGER_CONSTRAINT carries these verbatim into the
# chosen live SDK surface; an inline rephrase/compression drops them, which
# ADR-111 §Alternatives B rejected (the concrete examples ARE the regression
# detector). Dev may adjust a fingerprint WITH a matching test update and
# Architect sign-off — same convention as test_57_4 / test_confidence_gate.
# ---------------------------------------------------------------------------

# The 2026-05-03 anti-deferral fingerprint (also pinned in CONFRONTATION_TRIGGER_CONSTRAINT
# by test_57_4::test_each_constant_carries_its_load_bearing_fingerprint).
ANTIDEFER_FINGERPRINT = "Do NOT defer it to the"

# The social-side fingerprint: social-pack triggers (scandal / auction / trial)
# are as binding as a weapon drawn. Serves the social packs (tea_and_murder,
# pulp_noir) — the part most likely to be dropped if the migration only carries
# the combat guidance.
SOCIAL_FINGERPRINT = "exactly as mechanically binding as a weapon drawn"


def _live_sdk_confrontation_surfaces() -> dict[str, str]:
    """Every live SDK-path surface where confrontation-trigger steering could
    legitimately reach the model.

    Option-agnostic: covers the IntentRouter system prompt (Option B owner),
    every registered tool's description (Option A owner), and the slimmed
    sidecar (the home the other guardrails migrated to). The audit decision
    (A vs B) does not change WHICH surfaces are candidates — only which one
    ends up carrying the prose.
    """
    surfaces: dict[str, str] = {
        "intent_router._SYSTEM_PROMPT": _SYSTEM_PROMPT,
        "narrator_output_only_sidecar": NARRATOR_OUTPUT_ONLY,
    }
    for td in default_registry.tool_definitions():
        surfaces[f"tool:{td.name}.description"] = td.description
    return surfaces


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


# ---------------------------------------------------------------------------
# AC-1 / AC-2 regression guard — confrontation-trigger steering must reach the
# model on the SDK path. RED today: the fingerprints reach no live surface.
# ---------------------------------------------------------------------------


def test_antidefer_fingerprint_is_pinned_in_the_source_constant() -> None:
    """Guard: the fingerprint this file searches for is genuinely the
    constant's text. If someone edits CONFRONTATION_TRIGGER_CONSTRAINT without
    updating this file, fail here (loud) rather than silently searching for a
    phrase that no longer exists.
    """
    assert ANTIDEFER_FINGERPRINT in CONFRONTATION_TRIGGER_CONSTRAINT, (
        f"{ANTIDEFER_FINGERPRINT!r} is no longer in CONFRONTATION_TRIGGER_CONSTRAINT — "
        "update this test's fingerprint to match the constant's current anti-deferral rule."
    )
    assert SOCIAL_FINGERPRINT in CONFRONTATION_TRIGGER_CONSTRAINT, (
        f"{SOCIAL_FINGERPRINT!r} is no longer in CONFRONTATION_TRIGGER_CONSTRAINT — "
        "update this test's social fingerprint to match the constant."
    )


def test_confrontation_antidefer_steering_reaches_a_live_sdk_surface() -> None:
    """AC-1/AC-2: the anti-deferral trigger rule — fire the confrontation on the
    SAME turn the trigger appears, never defer it — MUST reach the model on the
    SDK path through at least one live surface.

    This is THE 2026-05-03 regression fingerprint: the narrator (now the router)
    must be told that a stake-binding beat commits the encounter THIS turn, or
    the dead-prose bug class reopens. Today it reaches no live surface → RED.
    """
    surfaces = _live_sdk_confrontation_surfaces()
    carriers = [name for name, text in surfaces.items() if ANTIDEFER_FINGERPRINT in text]
    assert carriers, (
        f"The confrontation anti-deferral steering ({ANTIDEFER_FINGERPRINT!r}) reaches NO "
        f"live SDK-path surface. Searched {len(surfaces)} surfaces: "
        f"{sorted(surfaces)}. On the default anthropic_sdk backend the "
        "CONFRONTATION_TRIGGER_CONSTRAINT is suppressed in build_narrator_prompt "
        "and was never migrated (test_57_4 line 337-340 removed its migration "
        "assertion). Migrate the steering to the SDK-path owner (the IntentRouter "
        "confrontation steering, or a confrontation/encounter tool description) so "
        "the 2026-05-03 dead-prose bug class cannot silently reopen."
    )


def test_confrontation_social_trigger_steering_reaches_a_live_sdk_surface() -> None:
    """AC-1/AC-2: the social-pack trigger steering — a scandal in print / a writ
    served / an auctioneer calling the lot is as mechanically binding as a weapon
    drawn — MUST reach the model on the SDK path too.

    Paranoia: migrating only the combat-side trigger guidance and dropping the
    social side would leave tea_and_murder / pulp_noir confrontations
    un-steered. Today this reaches no live surface → RED.
    """
    surfaces = _live_sdk_confrontation_surfaces()
    carriers = [name for name, text in surfaces.items() if SOCIAL_FINGERPRINT in text]
    assert carriers, (
        f"The social-pack confrontation steering ({SOCIAL_FINGERPRINT!r}) reaches NO "
        f"live SDK-path surface. Searched {len(surfaces)} surfaces: "
        f"{sorted(surfaces)}. The social-side triggers (scandal / trial / auction / "
        "social_duel) are exactly the part most likely to be dropped — migrate them "
        "alongside the combat triggers so social packs keep their confrontation steering."
    )


# ---------------------------------------------------------------------------
# AC-2 edge case — the owning subsystem ACTUALLY engages on a real dispatch
# (not merely registered). Drives the real bank → real confrontation engager →
# snapshot.encounter, then confirms the engagement watcher sees no mismatch.
# Expected GREEN: this is the regression guard for the Option-B owner (the
# engager/watcher were wired by 59-4). It proves the owner works; the
# steering tests above prove the owner is actually STEERED.
# ---------------------------------------------------------------------------


def _combat_pack() -> Any:
    """A minimal confrontation-capable pack (one ``combat`` ConfrontationDef).

    Mirrors ``synthetic_two_dial_pack`` in tests/server/conftest.py, inlined so
    this agents-package test is self-contained. MagicMock(spec=GenrePack) with a
    real RulesConfig lets the encounter engine look up the def without loading a
    full pack from disk.
    """
    from unittest.mock import MagicMock

    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ),
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


@pytest.mark.asyncio
async def test_router_dispatched_confrontation_actually_engages_encounter() -> None:
    """AC-2 edge case: a high-confidence ``confrontation`` dispatch routed through
    the REAL ``run_dispatch_bank`` engages the confrontation engine — an actual
    StructuredEncounter lands on the snapshot — and the engagement watcher
    confirms it (no confrontation mismatch).

    This is the "owning subsystem actually engages on a real trigger, not just
    that the handler is registered" proof the story's AC context calls out. It
    guards the Option-B owner (router → bank → engager, wired by 59-4) against
    regression.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    pack = _combat_pack()
    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    dispatch = SubsystemDispatch(
        subsystem="confrontation",
        params={"type": "combat"},
        depends_on=[],
        idempotency_key="c1",
        visibility=_tag_all(),
        confidence=0.95,  # >= 0.6 default gate → engages
    )
    pkg = DispatchPackage(
        turn_id="t-61-18",
        per_player=[
            PlayerDispatch(
                player_id="player:Sam",
                raw_action="I draw my blade on the bandit",
                resolved=[],
                dispatch=[dispatch],
                lethality=[],
                narrator_instructions=[],
            )
        ],
        cross_player=[],
        confidence_global=0.95,
    )
    context = {
        "snapshot": snap,
        "pack": pack,
        "player_name": "Sam",
        # Explicit opponent so the lifecycle helper seats an Other (ADR-116);
        # an explicit npcs_present is authoritative over the location fallback.
        "npcs_present": [NpcMention(name="Bandit", side="opponent", role="hostile")],
    }

    await run_dispatch_bank(pkg, context=context)

    assert snap.encounter is not None, (
        "router-dispatched confrontation did NOT engage: snapshot.encounter is still None "
        "after run_dispatch_bank routed a high-confidence confrontation dispatch. The "
        "owning subsystem (run_confrontation_dispatch) must instantiate the encounter "
        "BEFORE the narrator runs (ADR-113)."
    )
    assert snap.encounter.encounter_type == "combat", (
        f"engaged encounter has the wrong type: {snap.encounter.encounter_type!r} != 'combat'"
    )

    mismatches = detect_dispatch_engagement_mismatch(package=pkg, snapshot=snap)
    confrontation_mismatches = [m for m in mismatches if m.subsystem == "confrontation"]
    assert confrontation_mismatches == [], (
        "the dispatch-engagement watcher reports a confrontation mismatch even though the "
        f"engine engaged: {confrontation_mismatches}. The watcher is the GM-panel lie "
        "detector — a mismatch here means the engager and the witness disagree."
    )
