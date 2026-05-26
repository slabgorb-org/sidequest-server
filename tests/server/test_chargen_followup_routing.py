"""Content-free regression: phase=scene freeform input during AwaitingFollowup.

Playtest 2026-05-26 [road_warrior/the_circuit] — answering a free-text chargen
followup (the builder's ``hook_prompt`` re-prompt) hard-blocked character
creation with ``WrongPhaseError(expected=InProgress, got=AwaitingFollowup)``:
``_chargen_scene`` routed every freeform answer to ``apply_freeform`` (an
InProgress-only builder action), even when the builder had transitioned to
``AwaitingFollowup``. The builder already had the correct sink
(``answer_followup``); the handler just never called it.

These tests drive ``WebSocketSessionHandler._chargen_scene`` directly with a
synthetic builder (no genre pack, no WebSocket) so the routing is exercised
content-free. A second scene follows the hook so answering the followup lands
back in ``InProgress`` (not ``Confirmation``), keeping ``_next_message`` off the
pack-dependent confirmation-summary path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from opentelemetry import trace

from sidequest.game.builder import CharacterBuilder, HookType
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.game.test_builder_walk import make_choice, make_scene, simple_rules

_FOLLOWUP_PROMPT = "Give your rider a road name and your rig a name. Both matter."


def _builder_awaiting_followup() -> CharacterBuilder:
    """A builder driven to AwaitingFollowup on scene 0, with scene 1 waiting.

    Scene 0 carries a ``hook_prompt`` so applying its choice transitions to
    AwaitingFollowup; scene 1 is a plain choice scene so answering the followup
    advances back into InProgress (not Confirmation)."""
    scenes = [
        make_scene(
            "names",
            choices=[make_choice("Drifter", race_hint="Human")],
            hook_prompt=_FOLLOWUP_PROMPT,
        ),
        make_scene(
            "class",
            choices=[make_choice("Rider", class_hint="Rider")],
        ),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=simple_rules())
    builder.apply_choice(0)
    assert builder.is_awaiting_followup(), "fixture precondition: AwaitingFollowup"
    return builder


def _chargen_scene(builder: CharacterBuilder, answer: str, tmp_path: Path) -> list[object]:
    handler = WebSocketSessionHandler(save_dir=tmp_path)
    payload = CharacterCreationPayload(phase="scene", choice=answer)
    # sd is only dereferenced by _next_message on the Confirmation branch, which
    # this fixture deliberately avoids (a second scene follows the hook).
    sd = cast("Any", SimpleNamespace())
    return handler._chargen_scene(builder, payload, sd, "p1", trace.get_current_span())


def test_freeform_answer_in_awaiting_followup_does_not_wrongphase(tmp_path: Path) -> None:
    """The reported hard-block: a prose followup answer must NOT error out."""
    builder = _builder_awaiting_followup()

    out = _chargen_scene(
        builder, "They call me Rei. The bike's the Kingfisher — blue tank.", tmp_path
    )

    assert not any(isinstance(m, ErrorMessage) for m in out), (
        "freeform followup answer was rejected — the WrongPhaseError block is back"
    )
    # Advanced out of the followup, back into the normal scene walk.
    assert not builder.is_awaiting_followup()
    assert builder.is_in_progress()
    assert isinstance(out[0], CharacterCreationMessage)


def test_followup_answer_recorded_as_wound_hook(tmp_path: Path) -> None:
    """The answer is captured (answer_followup inserts it as a WOUND hook)."""
    builder = _builder_awaiting_followup()
    answer = "Rei, riding the Kingfisher."

    _chargen_scene(builder, answer, tmp_path)

    hooks = [h for r in builder.scene_results() for h in r.hooks_added]
    assert any(h.hook_type == HookType.WOUND and h.text == answer for h in hooks), (
        "followup answer was not routed to answer_followup / not recorded"
    )


def test_numeric_input_in_awaiting_followup_is_treated_as_followup_text(
    tmp_path: Path,
) -> None:
    """A numeric-looking answer in AwaitingFollowup is the followup text, not a
    choice index — it must not fall back to apply_choice (also InProgress-only)."""
    builder = _builder_awaiting_followup()

    out = _chargen_scene(builder, "1", tmp_path)

    assert not any(isinstance(m, ErrorMessage) for m in out)
    assert builder.is_in_progress()
    hooks = [h for r in builder.scene_results() for h in r.hooks_added]
    assert any(h.hook_type == HookType.WOUND and h.text == "1" for h in hooks)
