"""Regression: dynamic NarratorDirectives never leak machinery into the prompt.

Playtest 2026-06-19 [BUG] (sq-playtest-pingpong): the narrator emitted its own
constraint reasoning to the PLAYER verbatim — "The must_not_narrate constraint is
clear — ... I cannot narrate Roy picking up ... an envelope that doesn't exist."
Root cause: the orchestrator rendered each directive as ``- [{kind}] {payload}``,
exposing the raw NarratorDirectiveKind token (``must_not_narrate``) in the prompt
with no framing that these are SILENT stage directions, so the model parroted the
token and broke frame.

These tests pin the fix at the rendering boundary: the raw kind token is NEVER in
the rendered block, the payload is preserved, and the framing tells the narrator
to obey in-fiction and never break frame.
"""

from __future__ import annotations

from typing import get_args

from sidequest.agents.narrator_directives import (
    _DIRECTIVE_IMPERATIVES,
    render_narrator_directives,
)
from sidequest.protocol.dispatch import (
    NarratorDirective,
    NarratorDirectiveKind,
    VisibilityTag,
)


def _viz() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def test_every_kind_maps_to_an_imperative() -> None:
    """No NarratorDirectiveKind may be unmapped — fail loud, not silent."""
    assert set(_DIRECTIVE_IMPERATIVES) == set(get_args(NarratorDirectiveKind))


def test_no_machinery_token_leaks_for_any_kind() -> None:
    """The raw kind token must never appear; the payload always does."""
    for kind in get_args(NarratorDirectiveKind):
        directive = NarratorDirective(kind=kind, payload="PAYLOAD_SENTINEL", visibility=_viz())
        out = render_narrator_directives([directive])
        assert kind not in out, f"raw machinery token {kind!r} leaked into the prompt"
        assert "PAYLOAD_SENTINEL" in out
        assert _DIRECTIVE_IMPERATIVES[kind] in out


def test_must_not_narrate_renders_in_fiction_imperative() -> None:
    """The exact leak case: a must_not_narrate directive carries no token."""
    directive = NarratorDirective(
        kind="must_not_narrate",
        payload="inventing an off-screen responder",
        visibility=_viz(),
    )
    out = render_narrator_directives([directive])
    assert "must_not_narrate" not in out
    assert _DIRECTIVE_IMPERATIVES["must_not_narrate"] in out
    assert "inventing an off-screen responder" in out


def test_framing_directs_obey_in_fiction_and_no_break_frame() -> None:
    """The block must tell the narrator to stay in fiction and not break frame."""
    out = render_narrator_directives(
        [NarratorDirective(kind="must_narrate", payload="x", visibility=_viz())]
    )
    assert "stage-directions" in out  # framing wrapper present
    lowered = out.lower()
    # obey-silently + stay-in-world guidance (the anti-"I cannot narrate" steer)
    assert "story" in lowered or "fiction" in lowered
    assert "break frame" in lowered


def test_empty_directives_render_empty() -> None:
    """No directives → empty string (caller skips registering the section)."""
    assert render_narrator_directives([]) == ""
