"""Failing tests for Story 37-44: NPC identity drift across turns.

The bug: NPCs extracted from narrator output land in ``snapshot.npc_pool``
(that part already works — see ``test_apply_npc_registry_new_npc`` in
``test_dispatch.py``), but the pool was **never injected back into the
narrator prompt**. ``TurnContext`` carries ``npc_pool`` into the
orchestrator, but ``Orchestrator.build_narrator_prompt`` did not render it
as a prompt section. Result: every turn the narrator sees no canonical
identity data and reinvents name/pronouns/role from thin air.

Story 45-52 cleanup: this test file used to reference the legacy
``npc_registry``; the canonical store post-Wave-2A is ``npc_pool``.

Playtest 3 (2026-04-19, Felix Surrone, aureate_span):
  - Frandrew introduced round 17 as "she/her, captain-level"
  - Round 21 narrator demoted her to "junior/assistant, grease monkey"
  - Round 22 snapshot: "Prefect Frandrew Andrew (grease monkey, on ladder)"
  - Later rounds: "he/him, his usual brightness"

Acceptance criteria covered here:
  - AC-2: NPC dossier injection into prompt context (the primary wire gap)
  - AC-3: OTEL observability for auto-register and identity drift
  - AC-4: Wire-first boundary test — turn N extraction survives into turn N+1 prompt
  - AC-5: Multi-turn persistence

AC-1 (auto-population of ``npc_registry`` from ``npcs_present``) already has
coverage in ``tests/server/test_dispatch.py``; we add the OTEL span check here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import (
    NarrationTurnResult,
    NpcMention,
    Orchestrator,
    TurnContext,
)
from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    SectionCategory,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot
from sidequest.server.session_handler import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator() -> Orchestrator:
    client = MagicMock(spec=ClaudeClient)
    return Orchestrator(client=client)


def _frandrew_captain() -> NpcPoolMember:
    """The canonical identity the narrator must not drift away from."""
    return NpcPoolMember(
        name="Frandrew",
        role="captain",
        pronouns="she/her",
        appearance="tall, scarred eyebrow, grease-stained jacket",
        drawn_from="legacy_registry",
    )


async def _build_prompt_with_registry(
    registry_entries: list[NpcPoolMember],
) -> tuple[str, object]:
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npc_pool=registry_entries,
    )
    return await orch.build_narrator_prompt("look around", context)


# ---------------------------------------------------------------------------
# AC-2: NPC dossier injection into prompt context (the wire gap)
# ---------------------------------------------------------------------------


async def test_npc_registry_renders_as_prompt_section():
    """When ``TurnContext.npc_pool`` is non-empty, the built prompt must
    include a dossier section listing each known NPC. This is the root-cause
    fix for identity drift: the narrator can only stay consistent if it sees
    the canonical roster every turn.
    """
    prompt, _ = await _build_prompt_with_registry([_frandrew_captain()])

    # The name must appear in the prompt
    assert "Frandrew" in prompt, (
        "NPC dossier not injected: 'Frandrew' missing from prompt even though "
        "she is in the registry. This is the playtest-3 drift bug."
    )


async def test_npc_dossier_contains_canonical_pronouns():
    """Canonical pronouns must reach the narrator. Without them, the narrator
    defaults to whatever pronoun the last mention of the name happened to use.
    """
    prompt, _ = await _build_prompt_with_registry([_frandrew_captain()])
    assert "she/her" in prompt, (
        "Canonical pronouns missing from prompt — this is how Frandrew drifted "
        "from 'she/her captain' to 'he/him grease monkey' in 10 turns."
    )


async def test_npc_dossier_contains_canonical_role():
    """Role must reach the narrator — otherwise the narrator re-guesses."""
    prompt, _ = await _build_prompt_with_registry([_frandrew_captain()])
    assert "captain" in prompt.lower(), (
        "Canonical role missing — narrator will re-guess role each turn."
    )


async def test_npc_dossier_contains_canonical_appearance():
    """Appearance details must reach the narrator for visual consistency."""
    prompt, _ = await _build_prompt_with_registry([_frandrew_captain()])
    # Pick a distinctive appearance token that can't coincidentally appear
    assert "scarred eyebrow" in prompt, (
        "Appearance detail missing — narrator will invent new physical traits."
    )


async def test_empty_npc_registry_produces_no_dossier_section():
    """Zero-byte leak: if no NPCs are registered, no dossier section should
    be added to the prompt. Story 42-3 introduced this discipline (PacingHint)
    and it applies here too — pay only when the dossier has content.
    """
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npc_pool=[],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)

    agent_name = orch._narrator.name()
    section_names = {s.name for s in registry.registry(agent_name)}
    assert "npc_roster" not in section_names, (
        "Empty registry still produced npc_roster section — violates zero-byte-leak discipline."
    )


async def test_npc_roster_section_uses_valley_or_early_zone():
    """The roster is reference data, not primacy-zone identity. Per the
    prompt_framework zoning convention, background context belongs in
    Valley (lower attention); acute rules belong in Early/Primacy. Accept
    either Early or Valley — both are defensible; Primacy is not.
    """
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npc_pool=[_frandrew_captain()],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)

    agent_name = orch._narrator.name()
    roster_sections = [s for s in registry.registry(agent_name) if s.name == "npc_roster"]
    assert len(roster_sections) == 1, (
        f"Expected exactly one npc_roster section, got {len(roster_sections)}"
    )
    zone = roster_sections[0].zone
    assert zone in (AttentionZone.Early, AttentionZone.Valley), (
        f"npc_roster zone={zone!r} — should be Early or Valley (background reference), not Primacy."
    )


async def test_npc_roster_section_is_state_category():
    """Roster content describes current world state — not identity, genre,
    or format. Category should be ``SectionCategory.State``.
    """
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        npc_pool=[_frandrew_captain()],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)

    agent_name = orch._narrator.name()
    roster_sections = [s for s in registry.registry(agent_name) if s.name == "npc_roster"]
    assert len(roster_sections) == 1
    assert roster_sections[0].category == SectionCategory.State


async def test_multiple_npcs_all_rendered():
    """When the registry holds several NPCs, every one must reach the prompt.
    Playtest 3 had Frandrew (33), Vey (25), Marrien (6), Prefect But (2),
    Tchesla (1) — a real roster. Losing any of them is drift.
    """
    entries = [
        NpcPoolMember(
            name="Frandrew", role="captain", pronouns="she/her", drawn_from="legacy_registry"
        ),
        NpcPoolMember(name="Vey", role="engineer", pronouns="he/him", drawn_from="legacy_registry"),
        NpcPoolMember(
            name="Marrien", role="scout", pronouns="they/them", drawn_from="legacy_registry"
        ),
    ]
    prompt, _ = await _build_prompt_with_registry(entries)
    for name in ("Frandrew", "Vey", "Marrien"):
        assert name in prompt, f"{name} missing from multi-NPC roster"
    # Correct pronouns must survive for each
    assert "she/her" in prompt
    assert "he/him" in prompt
    assert "they/them" in prompt


# ---------------------------------------------------------------------------
# AC-4: Wire-first boundary test — pool write in turn N survives into
# the turn N+1 prompt. Exercises the full wire:
#   narrator output (NpcMention) → _apply_narration_result_to_snapshot
#   → snapshot.npc_pool → TurnContext(npc_pool=...)
#   → Orchestrator.build_narrator_prompt → prompt text
# ---------------------------------------------------------------------------


async def test_wiring_turn_n_registry_lands_in_turn_n_plus_1_prompt():
    """End-to-end wire: a narrator that introduces Frandrew as a she/her
    captain in turn N must have those exact canonical fields appear in the
    prompt built for turn N+1.
    """
    # Turn N — narrator returns an NpcMention in game_patch
    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
    )
    narration_n = NarrationTurnResult(
        narration="Frandrew looks up from the console. 'Prep undock,' she says.",
        npcs_present=[
            NpcMention(
                name="Frandrew",
                role="captain",
                pronouns="she/her",
                appearance="tall, scarred eyebrow",
            )
        ],
        is_degraded=False,
    )
    _apply_narration_result_to_snapshot(snapshot, narration_n, "Felix", room=room_for(snapshot))

    # Turn N+1 — TurnContext is rebuilt from snapshot and prompt is assembled.
    # If the wire is closed, Frandrew's canonical identity must appear.
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npc_pool=list(snapshot.npc_pool),
    )
    prompt_n_plus_1, _ = await orch.build_narrator_prompt("I salute the captain", context)

    assert "Frandrew" in prompt_n_plus_1
    assert "she/her" in prompt_n_plus_1
    assert "captain" in prompt_n_plus_1.lower()


# ---------------------------------------------------------------------------
# AC-5: Multi-turn persistence — identity stable across 3+ turns
# ---------------------------------------------------------------------------


async def test_multi_turn_registry_persistence_in_prompt():
    """Across three consecutive turns the registry is built up and each
    subsequent prompt must still carry every prior NPC's canonical identity.
    This directly mirrors the playtest-3 pattern that produced drift.
    """
    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
    )

    # Turn 1: introduce Frandrew as she/her captain
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Frandrew is on the bridge.",
            npcs_present=[NpcMention(name="Frandrew", role="captain", pronouns="she/her")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )
    # Turn 2: introduce Vey
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Vey slides under a console.",
            npcs_present=[NpcMention(name="Vey", role="engineer", pronouns="he/him")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )
    # Turn 3: narrator only re-mentions Frandrew by bare name.
    # The dossier must still carry she/her into the prompt for turn 4.
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Frandrew glances over.",
            npcs_present=[NpcMention(name="Frandrew")],  # bare, no pronouns
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    # Build turn-4 prompt and verify identity stability
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npc_pool=list(snapshot.npc_pool),
    )
    prompt, _ = await orch.build_narrator_prompt("I nod to Frandrew", context)

    # Both NPCs should appear in the roster
    assert "Frandrew" in prompt
    assert "Vey" in prompt
    # Canonical pronouns preserved from first mention, not overwritten by
    # later bare-name mention
    assert "she/her" in prompt, (
        "Bare-name re-mention overwrote canonical pronouns — identity drift."
    )
    assert "he/him" in prompt
    # Roles preserved
    assert "captain" in prompt.lower()
    assert "engineer" in prompt.lower()


def test_bare_name_re_mention_does_not_overwrite_canonical_fields():
    """If the narrator later mentions an NPC by bare name (empty role /
    pronouns / appearance), we must NOT overwrite the canonical data on
    the registry entry. Only additive updates are allowed.
    """
    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[
            NpcPoolMember(
                name="Frandrew",
                role="captain",
                pronouns="she/her",
                appearance="tall, scarred eyebrow",
                drawn_from="legacy_registry",
            )
        ],
    )
    # Narrator re-mentions Frandrew with no identity fields — likely the
    # common case once the name is known.
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Frandrew shrugs.",
            npcs_present=[NpcMention(name="Frandrew")],  # no role/pronouns
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    entry = snapshot.npc_pool[0]
    assert entry.role == "captain", "Bare-name re-mention wiped the role — identity drift bug."
    assert entry.pronouns == "she/her", "Bare-name re-mention wiped pronouns — identity drift bug."
    assert entry.appearance == "tall, scarred eyebrow"


# ---------------------------------------------------------------------------
# AC-3: OTEL observability — auto-registration + drift detection
# ---------------------------------------------------------------------------


def test_npc_auto_registered_span_is_defined_in_catalog():
    """Per the OTEL Observability Principle in CLAUDE.md, every subsystem
    fix must emit OTEL so the GM panel can tell the subsystem engaged
    (vs. Claude improvising). Auto-registration needs a dedicated span.
    """
    from sidequest.telemetry import spans as spans_module

    assert hasattr(spans_module, "SPAN_NPC_AUTO_REGISTERED"), (
        "SPAN_NPC_AUTO_REGISTERED missing from telemetry catalog — "
        "without it the GM panel can't tell whether NPC auto-registration "
        "ran this turn or whether Claude is faking consistency."
    )
    assert spans_module.SPAN_NPC_AUTO_REGISTERED == "npc.auto_registered", (
        "Span name must be exactly 'npc.auto_registered' for the GM panel filter to match."
    )


def test_npc_reinvented_span_is_defined_in_catalog():
    """The drift-detector span name must be stable so the GM panel can
    surface warnings. ``npc.reinvented`` fires when narrator pronouns / role
    diverge from the registry.
    """
    from sidequest.telemetry import spans as spans_module

    assert hasattr(spans_module, "SPAN_NPC_REINVENTED"), (
        "SPAN_NPC_REINVENTED missing — no drift visibility. The GM panel "
        "cannot distinguish narrator drift from deliberate reveal without it."
    )
    assert spans_module.SPAN_NPC_REINVENTED == "npc.reinvented"


def test_auto_register_emits_span_on_new_npc(caplog, monkeypatch):
    """When a new NPC lands in the registry, the code path must log at a
    level that a GM watching the panel can see (info or warn, not debug).

    NOTE: This test accepts either a real OTEL span or a structured log line
    that the OTEL exporter will pick up — the concrete implementation is
    Dev's choice. What matters is that ``npc.auto_registered`` (or the
    equivalent logger event) fires on a new-NPC path.
    """
    import logging

    # app.py disables propagation on the sidequest logger at import time
    # (so uvicorn's dictConfig doesn't silence us). pytest's caplog attaches
    # at the root logger, so re-enable propagation for the duration of this test.
    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
    )
    with caplog.at_level(logging.INFO):
        _apply_narration_result_to_snapshot(
            snapshot,
            NarrationTurnResult(
                narration="A newcomer arrives.",
                npcs_present=[
                    NpcMention(
                        name="Frandrew",
                        role="captain",
                        pronouns="she/her",
                    )
                ],
                is_degraded=False,
            ),
            "Felix",
            room=room_for(snapshot),
        )

    all_logs = caplog.text
    assert "npc.auto_registered" in all_logs, (
        "Auto-registration produced no `npc.auto_registered` event. The GM "
        "panel filter won't see anything fire. Story 37-44 AC-3."
    )
    # Identity fields must be present in the event so the panel can distinguish
    # first-registration from a later no-op mention.
    assert "she/her" in all_logs and "captain" in all_logs, (
        "`npc.auto_registered` event fired but carried no pronouns/role — "
        "panel cannot verify the registration captured canonical identity."
    )


def test_drift_detector_exists_as_callable():
    """A drift detector must exist — compare narrator output pronouns
    against registry pronouns and emit ``npc.reinvented`` when they
    disagree. The function should live in the session_handler or a
    dedicated npc module so it can be called from the narration apply path.
    """
    # The canonical name is up to Dev, but it must exist somewhere reachable
    # from the session handler. Probe the likely locations.
    from sidequest.server import session_handler

    candidates = [
        "_detect_npc_identity_drift",
        "_check_npc_identity_drift",
        "detect_npc_drift",
        "_warn_on_npc_drift",
    ]
    found = [c for c in candidates if hasattr(session_handler, c)]
    assert found, (
        "No drift detector found in session_handler. Expected one of: "
        f"{candidates}. Without it, pronoun/role drift goes unreported "
        "and we lose the 'OTEL is the lie detector' guarantee."
    )


def test_drift_detector_fires_on_pronoun_mismatch(caplog, monkeypatch):
    """When narrator output mentions an NPC with pronouns that disagree
    with the canonical registry entry, a ``npc.reinvented`` event must
    fire. Using caplog here because the detector can log-with-span or
    pure-log; both are acceptable wiring.
    """
    import logging

    # See comment in test_auto_register_emits_span_on_new_npc — app.py
    # disables propagation on the sidequest logger at import time.
    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[
            NpcPoolMember(
                name="Frandrew",
                role="captain",
                pronouns="she/her",
                drawn_from="legacy_registry",
            )
        ],
    )
    # Narrator now says Frandrew is he/him — this is drift
    with caplog.at_level(logging.WARNING):
        _apply_narration_result_to_snapshot(
            snapshot,
            NarrationTurnResult(
                narration="Frandrew scratches his neck.",
                npcs_present=[NpcMention(name="Frandrew", pronouns="he/him")],
                is_degraded=False,
            ),
            "Felix",
            room=room_for(snapshot),
        )

    all_logs = caplog.text
    assert "npc.reinvented" in all_logs, (
        "Drift detector did not fire when pronouns changed she/her → he/him. "
        "This is the exact Frandrew drift from playtest-3."
    )


def test_explicit_drift_does_not_overwrite_canonical_pronouns(caplog, monkeypatch):
    """After drift is detected, the canonical registry field must stay frozen.
    The Frandrew scenario: turn 17 registers her as she/her captain; turn 21
    the narrator says he/him grease monkey. The drift detector fires a warning
    — and then the upsert path MUST NOT silently overwrite the canonical
    pronouns/role with the drifted values. Otherwise the warning fires once,
    the drifted value becomes canonical, and subsequent drift goes undetected.
    This is reviewer finding [CRITICAL] from the 37-44 review.
    """
    import logging

    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[
            NpcPoolMember(
                name="Frandrew",
                role="captain",
                pronouns="she/her",
                appearance="tall, scarred eyebrow",
                drawn_from="legacy_registry",
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        _apply_narration_result_to_snapshot(
            snapshot,
            NarrationTurnResult(
                narration="Frandrew scratches his neck — 'grease monkey work,' he mutters.",
                npcs_present=[
                    NpcMention(
                        name="Frandrew",
                        pronouns="he/him",
                        role="grease monkey",
                        appearance="oil-streaked coveralls",
                    )
                ],
                is_degraded=False,
            ),
            "Felix",
            room=room_for(snapshot),
        )

    # Drift detector must have fired
    assert "npc.reinvented" in caplog.text, (
        "Drift detector did not fire — precondition for this test failed."
    )

    # Canonical fields MUST remain untouched
    entry = snapshot.npc_pool[0]
    assert entry.pronouns == "she/her", (
        f"Canonical pronouns overwritten by drifted value: got {entry.pronouns!r}, "
        "expected 'she/her'. Drift detection is theatre if the drifted value "
        "silently canonicalizes — on turn N+1 the narrator will see he/him "
        "in the roster and drift will be permanent."
    )
    assert entry.role == "captain", (
        f"Canonical role overwritten by drifted value: got {entry.role!r}, expected 'captain'."
    )
    assert entry.appearance == "tall, scarred eyebrow", (
        f"Canonical appearance overwritten: got {entry.appearance!r}."
    )


def test_drift_detector_fires_on_role_mismatch(caplog, monkeypatch):
    """The drift detector has an independent role branch. A pronoun-only test
    leaves this path unverified — a broken role-drift detector would ship
    without any test catching it.
    """
    import logging

    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[
            NpcPoolMember(
                name="Frandrew",
                role="captain",
                pronouns="she/her",
                drawn_from="legacy_registry",
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        _apply_narration_result_to_snapshot(
            snapshot,
            NarrationTurnResult(
                narration="Frandrew greases a bolt.",
                # Matching pronouns (no pronoun drift) but mismatched role.
                npcs_present=[
                    NpcMention(name="Frandrew", pronouns="she/her", role="grease monkey")
                ],
                is_degraded=False,
            ),
            "Felix",
            room=room_for(snapshot),
        )

    assert "npc.reinvented" in caplog.text
    assert "field=role" in caplog.text, (
        "Drift detector fired but did not identify `role` as the drifted field."
    )


def test_case_insensitive_comparison_does_not_fire_drift(caplog, monkeypatch):
    """`She/Her` vs `she/her` must NOT fire drift. Protects against accidental
    removal of `.lower()` normalization in the detector.
    """
    import logging

    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[
            NpcPoolMember(
                name="Frandrew",
                role="Captain",
                pronouns="She/Her",
                drawn_from="legacy_registry",
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        _apply_narration_result_to_snapshot(
            snapshot,
            NarrationTurnResult(
                narration="Frandrew nods.",
                npcs_present=[NpcMention(name="Frandrew", pronouns="she/her", role="captain")],
                is_degraded=False,
            ),
            "Felix",
            room=room_for(snapshot),
        )

    assert "npc.reinvented" not in caplog.text, (
        "Drift detector fired on case-only difference — case-insensitive comparison broken."
    )
