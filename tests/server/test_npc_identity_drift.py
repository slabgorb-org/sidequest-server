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


def test_explicit_drift_overwrites_canonical_pronouns_and_role(caplog, monkeypatch):
    """Story 72-7 REVERSES the old warn-only behavior. The Frandrew scenario:
    turn 17 registers her as she/her captain; turn 21 the narrator settles her
    as he/him grease monkey. Under 72-7 the narrator's correction is
    *authoritative* — the canonical pronouns/role are **overwritten** so the
    turn N+1 roster carries the corrected identity, not the first throwaway
    guess.

    This used to be ``test_explicit_drift_does_not_overwrite_canonical_pronouns``
    (37-44, which froze the canonical value). 72-7 makes drift apply, so the
    assertions flip: pronouns/role now move; ``appearance`` stays additive
    (a paraphrased description is accretion, not an identity correction).
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

    # Drift detector must still fire (it now records an *applied* overwrite).
    assert "npc.reinvented" in caplog.text, (
        "Drift detector did not fire — precondition for this test failed."
    )

    # Canonical identity fields MUST now carry the narrator's correction.
    entry = snapshot.npc_pool[0]
    assert entry.pronouns == "he/him", (
        f"72-7: canonical pronouns were not overwritten on re-mention: got "
        f"{entry.pronouns!r}, expected 'he/him'. The narrator's correction must "
        "stick so the turn N+1 roster is right."
    )
    assert entry.role == "grease monkey", (
        f"72-7: canonical role was not overwritten: got {entry.role!r}, "
        "expected 'grease monkey'."
    )
    # appearance is OUT of scope for overwrite — remains additive (fill-empty),
    # so the already-set value is preserved, not churned by paraphrase.
    assert entry.appearance == "tall, scarred eyebrow", (
        f"appearance must stay additive (not overwritten): got {entry.appearance!r}."
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


# ===========================================================================
# Story 72-7: Apply NPC identity drift authoritatively (overwrite, not warn-only)
#
# These tests drive ``_apply_narration_result_to_snapshot`` (the production
# entry that feeds ``_apply_npc_mentions``) with a synthetic snapshot + an
# ``NpcMention`` that disagrees with the canonical pool member, then assert on
# (a) the **mutated snapshot state** (the canonical value moved) and (b) the
# **emitted ``npc.reinvented`` span** carrying an ``applied`` marker + old→new.
# Span assertions use the ``otel_capture`` in-memory exporter (conftest), not
# log text, per server CLAUDE.md "No Source-Text Wiring Tests" and the story
# context ("assert via the span/watcher harness, not log text").
# ===========================================================================


def _reinvented_spans(exporter):
    """All captured ``npc.reinvented`` spans, in emission order."""
    return [s for s in exporter.get_finished_spans() if s.name == "npc.reinvented"]


def _drift_snapshot(member: NpcPoolMember) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[member],
    )


# --- AC-1: pronoun overwrite -------------------------------------------------


def test_drift_overwrites_canonical_pronouns(otel_capture):
    """AC-1. Canonical ``pronouns='they/them'`` is overwritten to ``'she/her'``
    when a later mention for the same case-folded name disagrees. (Today the
    additive-only upsert leaves it ``'they/them'`` forever — the session-894
    Sitä-minutta bug.)
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Sitä-minutta",
            role="floor-boss",
            pronouns="they/them",
            drawn_from="narrator_invented",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Sitä-minutta straightens. 'You're late,' she says.",
            npcs_present=[NpcMention(name="Sitä-minutta", pronouns="she/her")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    assert snapshot.npc_pool[0].pronouns == "she/her", (
        "72-7 AC-1: canonical pronouns were not overwritten on disagreeing "
        f"re-mention: got {snapshot.npc_pool[0].pronouns!r}, expected 'she/her'."
    )


# --- AC-2: role overwrite ----------------------------------------------------


def test_drift_overwrites_canonical_role(otel_capture):
    """AC-2. Canonical ``role='assistant'`` is overwritten to ``'captain'`` on a
    disagreeing re-mention (the narrator promotes the assistant to captain and
    the canonical record follows).
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Vey",
            role="assistant",
            pronouns="he/him",
            drawn_from="narrator_invented",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Captain Vey takes the bridge.",
            npcs_present=[NpcMention(name="Vey", role="captain")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    assert snapshot.npc_pool[0].role == "captain", (
        "72-7 AC-2: canonical role was not overwritten on disagreeing "
        f"re-mention: got {snapshot.npc_pool[0].role!r}, expected 'captain'."
    )


# --- AC-3: applied drift span (old -> new) -----------------------------------


def test_drift_applied_span_carries_applied_marker_and_old_new(otel_capture):
    """AC-3 (emission contract). Each authoritative overwrite emits the
    ``npc.reinvented`` span carrying ``expected`` (old), ``narrator`` (new),
    ``drift_field``, and an ``applied`` attribute distinguishing it from the
    prior warn-only emission. Today the span fires WITHOUT ``applied`` — this
    is the marker the GM panel uses to tell "canonical record moved" from
    "mismatch merely noticed".
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Sitä-minutta",
            role="floor-boss",
            pronouns="they/them",
            drawn_from="narrator_invented",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="'She,' Sitä-minutta corrects.",
            npcs_present=[NpcMention(name="Sitä-minutta", pronouns="she/her")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    spans = _reinvented_spans(otel_capture)
    assert len(spans) == 1, f"expected exactly one npc.reinvented span, got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("drift_field") == "pronouns", f"wrong drift_field: {attrs.get('drift_field')!r}"
    assert attrs.get("expected") == "they/them", f"old value not captured: {attrs.get('expected')!r}"
    assert attrs.get("narrator") == "she/her", f"new value not captured: {attrs.get('narrator')!r}"
    assert attrs.get("applied") is True, (
        "72-7 AC-3: span lacks the `applied=True` marker — the GM panel cannot "
        "tell the overwrite was APPLIED vs the old warn-only emission. "
        f"applied attribute = {attrs.get('applied')!r}."
    )


def test_npc_reinvented_route_projects_applied_marker():
    """AC-3 (projection contract). The GM panel reads the *routed*
    ``state_transition`` event, not the raw span. The
    ``SPAN_ROUTES[SPAN_NPC_REINVENTED]`` extractor must propagate the
    ``applied`` marker (and old/new) into the projected event dict, or the
    lie-detector goes dark on whether the record actually moved.
    """
    from sidequest.telemetry import spans as spans_module

    route = spans_module.SPAN_ROUTES[spans_module.SPAN_NPC_REINVENTED]

    class _FakeSpan:
        attributes = {
            "npc_name": "Sitä-minutta",
            "drift_field": "pronouns",
            "expected": "they/them",
            "narrator": "she/her",
            "turn_number": 2,
            "applied": True,
        }

    projected = route.extract(_FakeSpan())
    assert projected.get("applied") is True, (
        "72-7 AC-3: npc.reinvented route does not project the `applied` marker "
        f"into the GM-panel event: {projected!r}."
    )


# --- AC-4: bounded to identity fields; mechanical state + appearance untouched


def test_drift_overwrite_leaves_mechanical_state_and_appearance_untouched(otel_capture):
    """AC-4. A pronoun/role overwrite must not reach mechanical state
    (``disposition``) or churn ``appearance`` (which stays additive). Only the
    identity fields move.
    """
    from sidequest.game.disposition import Disposition

    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Vey",
            role="assistant",
            pronouns="they/them",
            appearance="oil-streaked coveralls",
            disposition=Disposition(25),
            drawn_from="narrator_invented",
        )
    )
    disp_before = int(snapshot.npc_pool[0].disposition)

    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Captain Vey, now in a pressed uniform, takes the bridge. 'She has it,'"
            " someone mutters.",
            npcs_present=[
                NpcMention(
                    name="Vey",
                    role="captain",
                    pronouns="she/her",
                    appearance="pressed command uniform",
                )
            ],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    entry = snapshot.npc_pool[0]
    # identity fields moved
    assert entry.pronouns == "she/her" and entry.role == "captain", (
        "precondition: identity overwrite must apply for this AC-4 test to be meaningful"
    )
    # appearance stayed additive (already set -> not overwritten by paraphrase)
    assert entry.appearance == "oil-streaked coveralls", (
        f"72-7 AC-4: appearance was overwritten (should stay additive): {entry.appearance!r}."
    )
    # disposition (mechanical) untouched
    assert int(entry.disposition) == disp_before == 25, (
        f"72-7 AC-4: disposition changed during identity overwrite: {int(entry.disposition)}."
    )


# --- AC-5: no-op when mention agrees or is empty (regression guards) ----------


def test_agreeing_mention_performs_no_overwrite_and_no_span(otel_capture):
    """AC-5. A re-mention that AGREES (case-insensitively) with canonical
    values performs no overwrite and emits NO ``npc.reinvented`` span. Guards
    the "empty = no opinion" / "agree = no drift" contract against accidental
    always-fire regressions once overwrite is wired.
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Vey",
            role="Captain",
            pronouns="She/Her",
            drawn_from="narrator_invented",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Vey nods.",
            npcs_present=[NpcMention(name="Vey", role="captain", pronouns="she/her")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    assert _reinvented_spans(otel_capture) == [], (
        "72-7 AC-5: agreeing re-mention emitted a npc.reinvented span — "
        "case-insensitive 'no drift' contract broken."
    )
    # canonical values preserved verbatim (no needless rewrite)
    assert snapshot.npc_pool[0].pronouns == "She/Her"
    assert snapshot.npc_pool[0].role == "Captain"


def test_empty_mention_fields_perform_no_overwrite_and_no_span(otel_capture):
    """AC-5. A bare-name re-mention (empty role/pronouns) is "no opinion": no
    overwrite, no span. (Companion to the existing
    ``test_bare_name_re_mention_does_not_overwrite_canonical_fields`` — adds the
    span-silence assertion.)
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Vey",
            role="captain",
            pronouns="she/her",
            drawn_from="narrator_invented",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Vey shrugs.",
            npcs_present=[NpcMention(name="Vey")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    assert _reinvented_spans(otel_capture) == [], (
        "72-7 AC-5: bare-name re-mention emitted a drift span — 'empty = no "
        "opinion' contract broken."
    )
    assert snapshot.npc_pool[0].pronouns == "she/her"
    assert snapshot.npc_pool[0].role == "captain"


# --- Edge: conflicting drift within one turn (last-mention-wins) --------------


def test_conflicting_drift_within_turn_last_mention_wins(otel_capture):
    """Edge. Two mentions of the same name in ONE turn carrying different new
    pronouns must resolve deterministically (last-mention-wins, matching the
    sequential apply loop) — the canonical record must not be left in an
    order-indeterminate state, and each applied step is observable as a span.
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Vey",
            pronouns="they/them",
            role="assistant",
            drawn_from="narrator_invented",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Vey enters, then corrects herself.",
            npcs_present=[
                NpcMention(name="Vey", pronouns="he/him"),
                NpcMention(name="Vey", pronouns="she/her"),
            ],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    assert snapshot.npc_pool[0].pronouns == "she/her", (
        "72-7 edge: conflicting same-turn drift did not resolve last-wins: "
        f"got {snapshot.npc_pool[0].pronouns!r}, expected 'she/her'."
    )
    # both applied steps must be observable (not collapsed to one)
    assert len(_reinvented_spans(otel_capture)) == 2, (
        "each applied overwrite step must emit its own npc.reinvented span; "
        f"got {len(_reinvented_spans(otel_capture))}."
    )


# --- Edge: player/world-authored identity is NOT overwritten by narrator drift


def test_drift_does_not_overwrite_world_authored_identity(otel_capture):
    """Edge (policy decision — TEA). Narrator drift must NOT silently overwrite
    a *human-authored* identity (``drawn_from='world_authored'`` — Jade/Keith
    wrote this NPC into the world pack). The overwrite is suppressed and the
    span records ``applied=False`` so the disagreement stays visible on the GM
    panel (No Silent Fallbacks). Narrator-sourced members (other ``drawn_from``)
    still overwrite — see AC-1/AC-2.
    """
    snapshot = _drift_snapshot(
        NpcPoolMember(
            name="Prefect Autelle",
            role="prefect",
            pronouns="they/them",
            drawn_from="world_authored",
        )
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="The prefect waves you off. 'She's busy,' an aide says.",
            npcs_present=[NpcMention(name="Prefect Autelle", pronouns="she/her")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    # canonical author-set identity is preserved
    assert snapshot.npc_pool[0].pronouns == "they/them", (
        "72-7 edge: narrator drift overwrote a world_authored identity: "
        f"got {snapshot.npc_pool[0].pronouns!r}, expected 'they/them' preserved."
    )
    # but the disagreement is still span-visible, marked NOT applied
    spans = _reinvented_spans(otel_capture)
    assert len(spans) == 1, f"world_authored drift must still emit a span; got {len(spans)}"
    assert dict(spans[0].attributes or {}).get("applied") is False, (
        "72-7 edge: suppressed (world_authored) drift must mark the span "
        "applied=False so the GM panel sees it was noticed-not-applied; got "
        f"{dict(spans[0].attributes or {}).get('applied')!r}."
    )


# --- Edge: overwriting to match another NPC's values does not merge entries ---


def test_drift_to_matching_values_does_not_merge_pool_entries(otel_capture):
    """Edge. Overwriting member B's pronouns to a value another member A already
    holds must NOT merge, alias, or cross-link the two pool entries — the join
    key is the (unchanged) case-folded name, so they remain two distinct
    members. Only B's identity field moves.
    """
    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        location="Bridge",
        npc_pool=[
            NpcPoolMember(name="Aria", pronouns="she/her", drawn_from="narrator_invented"),
            NpcPoolMember(name="Bex", pronouns="they/them", drawn_from="narrator_invented"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration="Bex laughs. 'She knows,' Bex says of herself.",
            npcs_present=[NpcMention(name="Bex", pronouns="she/her")],
            is_degraded=False,
        ),
        "Felix",
        room=room_for(snapshot),
    )

    assert len(snapshot.npc_pool) == 2, "overwrite must not drop/merge a pool entry"
    by_name = {m.name: m for m in snapshot.npc_pool}
    assert set(by_name) == {"Aria", "Bex"}, "names (join keys) must be unchanged and distinct"
    assert by_name["Bex"].pronouns == "she/her", "B's pronoun overwrite did not apply"
    assert by_name["Aria"].pronouns == "she/her", "A must be untouched by B's drift"
