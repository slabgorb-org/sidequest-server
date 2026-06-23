"""Render dynamic NarratorDirectives into player-safe stage directions.

Single source of truth for turning the dispatch bank's + lethality arbiter's
``NarratorDirective``s into the ``narrator_directives`` prompt section.

Playtest 2026-06-19 [BUG] (sq-playtest-pingpong): the narrator emitted its own
constraint reasoning to the PLAYER verbatim —

    "The must_not_narrate constraint is clear — no envelope has been established
     in this scene. I cannot narrate Roy picking up, examining, or opening an
     envelope that doesn't exist."

Root cause: the orchestrator rendered each directive as ``- [{kind}] {payload}``,
putting the raw ``NarratorDirectiveKind`` token (``must_not_narrate``) in the
prompt with no framing that these are SILENT stage directions. The model parroted
the token and broke frame to explain the constraint.

Fix (this module): every kind maps to an in-fiction imperative carrying NO
machinery vocabulary, and the lines are wrapped in framing that tells the
narrator to obey inside the story and never break frame. The narrator has no
token to echo and explicit instruction not to surface its reasoning.
"""

from __future__ import annotations

from typing import get_args

from sidequest.protocol.dispatch import NarratorDirective, NarratorDirectiveKind

# Each directive KIND becomes an in-fiction imperative — never the raw token.
# Deliberately avoids "narrate"/"constraint"/"directive" so the narrator is given
# no machinery vocabulary to echo into player-facing prose (the 2026-06-19 leak).
_DIRECTIVE_IMPERATIVES: dict[NarratorDirectiveKind, str] = {
    "must_narrate": "Bring into the scene",
    "must_not_narrate": "Keep out of the fiction",
    "distinctive_detail_for_referent": "Ground with a distinctive, specific detail",
    "canonical_only_do_not_reveal_to_others": (
        "True in the world, but hidden from the other players"
    ),
}

# Fail loud at import if a new NarratorDirectiveKind is added without an
# imperative — No Silent Fallbacks. A missing mapping must never silently fall
# back to leaking the raw token.
assert set(_DIRECTIVE_IMPERATIVES) == set(get_args(NarratorDirectiveKind)), (
    "every NarratorDirectiveKind must map to an in-fiction imperative in "
    "_DIRECTIVE_IMPERATIVES — add the new kind's player-safe phrasing"
)

# Framing wrapper. Tells the narrator these are stage directions to obey silently,
# and — the anti-"I cannot narrate" steer — that an unhonorable request is shown
# IN-WORLD (the hand finds only what is there) rather than by breaking frame.
_FRAMING_OPEN: str = (
    "<stage-directions>\n"
    "Silent stage directions for this turn — honor each one inside the story. "
    "Never quote, name, or explain them in your prose; the player reads only the "
    "fiction, never the stagecraft. If the player reached for something that is "
    "not here to give them, show that in the world — their hand finds only what is "
    "actually there — rather than break frame to say you cannot describe it."
)
_FRAMING_CLOSE: str = "\n</stage-directions>"


def render_narrator_directives(directives: list[NarratorDirective]) -> str:
    """Render directives into a player-safe, framed stage-direction block.

    Returns ``""`` when there are no directives (the caller then skips
    registering the section). Each line is an in-fiction imperative + the
    directive payload; the raw ``NarratorDirectiveKind`` token is NEVER emitted.
    Pure; no I/O.
    """
    if not directives:
        return ""
    lines = "\n".join(f"- {_DIRECTIVE_IMPERATIVES[d.kind]}: {d.payload}" for d in directives)
    return f"{_FRAMING_OPEN}\n{lines}{_FRAMING_CLOSE}"


__all__ = ["render_narrator_directives"]
