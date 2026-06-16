"""Story 97-5 (RED): the turn-1 double-apply of ``_apply_npc_mentions``.

ROOT CAUSE (measured, not assumed)
----------------------------------
A dice-gated player action runs ``_execute_narration_turn`` **twice** inside a
single interaction turn:

1. Pass 1 — the player action enters ``_execute_narration_turn`` (router runs,
   narrator asks for a roll, a ``DICE_THROW`` is emitted). The narration result
   is applied via ``_apply_narration_result_to_snapshot`` → ``_apply_npc_mentions``.
2. Pass 2 — the dice handler (``sidequest/handlers/dice_throw.py``) re-enters
   ``_execute_narration_turn`` with synthesized ``[BEAT_RESOLVED]`` /
   ``[DOGFIGHT_SHOT_RESOLVED]`` replay text and ``suppress_intent_router=True``.
   Story 91-2 ("Dark Spend") taught the *intent router* to skip this replay
   re-entry — the dice outcome carries no new player intent. But the
   ``_apply_npc_mentions`` seam at ``websocket_session_handler.py`` ~line 1094
   was **never** given the same replay guard, so every per-mention side effect
   (last_seen stamps, pool matching, mint paths, the disposition beat) runs a
   SECOND time on the replay pass.

This is the upstream source of the blackthorn solo 2026-06-07 turn-1 symptom:
two byte-identical disposition beats per NPC. Server #742 patched the
*development tick* harmless at the seam (``Npc.last_development_turn``
across-call dedupe), but the double-CALL itself was left in place and silently
double-runs every other mention side effect.

THE FIX (GREEN — for Dev)
-------------------------
Apply the Story 91-2 doctrine to the mention-apply: when
``_execute_narration_turn`` runs as a dice-resolution replay re-entry
(``suppress_intent_router=True``), do not re-apply NPC mentions — the scene's
NPCs were already applied on pass 1 and the replay introduces no new player
intent. Gate the ``_apply_npc_mentions`` call (or the mention step inside
``_apply_narration_result_to_snapshot``) on the replay condition. Then the
#742 seam-level ``last_development_turn`` dedupe becomes dead for its stated
across-call purpose — see the Delivery Finding for the removal/defense-in-depth
decision (and its sibling ``test_same_turn_double_apply_develops_once``).

These tests drive the REAL production method ``_execute_narration_turn`` (the
single-player ``session_handler_factory`` harness, narrator stubbed) and assert
on the runtime invocation of the production ``_apply_npc_mentions`` callable —
behavioral, not a source-text grep (CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import sidequest.server.narration_apply as narration_apply
from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.session import Npc


def _seat_stateful_npc(sd, name: str = "Boris") -> None:
    """Seat one existing stateful ``Npc`` so the mention resolves on the
    ``npcs_hit`` branch (a re-cite, the blackthorn shape) rather than minting."""
    sd.snapshot.npcs.append(
        Npc(
            core=CreatureCore(name=name, description="X.", personality="Y."),
            disposition=Disposition(0),
        )
    )


def _stub_narrator_naming(sd, name: str = "Boris", *, narration: str) -> None:
    """Stub the narrator turn to return a result that names ``name`` present."""
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration=narration,
            npcs_present=[NpcMention(name=name, side="neutral")],
        ),
    )


def _spy_apply_npc_mentions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap the production ``_apply_npc_mentions`` with a call counter that still
    delegates to the real implementation. Returns a 1-element list used as a
    mutable counter (``calls[0]``)."""
    real = narration_apply._apply_npc_mentions
    calls = [0]

    def _counting(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    # Patch the module-level name that ``_apply_narration_result_to_snapshot``
    # calls directly. Patching here (where it is looked up) is what makes the
    # spy reach the production call site.
    monkeypatch.setattr(narration_apply, "_apply_npc_mentions", _counting)
    return calls


@pytest.mark.asyncio
async def test_dice_replay_reentry_does_not_reapply_npc_mentions(
    session_handler_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """AC1 (RED): the dice-resolution replay re-entry of ``_execute_narration_turn``
    (``suppress_intent_router=True``) MUST NOT re-run ``_apply_npc_mentions``.

    Pre-fix this fails — the replay pass still applies mentions, so every
    per-mention side effect double-runs within one interaction turn (the
    blackthorn turn-1 double-write)."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seat_stateful_npc(sd, "Boris")
    _stub_narrator_naming(sd, "Boris", narration="[BEAT_RESOLVED] Boris steadies himself.")
    calls = _spy_apply_npc_mentions(monkeypatch)

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(  # noqa: SLF001 — testing internal seam
        sd,
        "[BEAT_RESOLVED] Boris steadies himself.",
        _build_turn_context(sd),
        suppress_intent_router=True,
    )

    assert calls[0] == 0, (
        "A dice-resolution replay re-entry (suppress_intent_router=True) must "
        "NOT re-apply NPC mentions — the scene's NPCs were already applied on "
        "the player-action pass. Re-applying here is the turn-1 double-apply "
        f"root cause. _apply_npc_mentions fired {calls[0]} time(s) on the replay."
    )


@pytest.mark.asyncio
async def test_normal_turn_applies_npc_mentions_exactly_once(
    session_handler_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """AC1 control: a NORMAL turn (not a replay re-entry) must still apply NPC
    mentions exactly once. Guards against the fix over-suppressing the live
    player-action path."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seat_stateful_npc(sd, "Boris")
    _stub_narrator_naming(sd, "Boris", narration="Boris meets your eye and nods.")
    calls = _spy_apply_npc_mentions(monkeypatch)

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(  # noqa: SLF001 — testing internal seam
        sd,
        "I greet Boris.",
        _build_turn_context(sd),
        suppress_intent_router=False,
    )

    assert calls[0] == 1, (
        "A normal narration turn must apply NPC mentions exactly once; "
        f"_apply_npc_mentions fired {calls[0]} time(s)."
    )
