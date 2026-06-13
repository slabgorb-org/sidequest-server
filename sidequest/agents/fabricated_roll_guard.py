"""Fabricated-roll guard — post-narration lie detector for invented dice.

The narrator never sees the engine's dice. Server-rolled events (an opponent's
reprisal, an opposed NPC check) resolve their d20 on the server and send the
dice *messages to the table*, not into the narrator prompt (see
``dispatch/dice.py`` — "the dice messages go to the table, not the prompt").
The narrator's own rolls happen through the ``roll_dice`` tool. Therefore any
roll/AC NUMBER that appears in player-facing prose when no dice tool fired this
turn is, by construction, a fabricated mechanic with zero engine backing —
"the worst lie the narrator can tell" (``narrator_prompts/output_only.md``).

This module is the detection half. ``detect_fabricated_roll`` scans finished
narration for that tell so the orchestrator can emit the
``narrator.fabricated_roll`` OTEL span (GM-panel lie detector) and reprompt a
clean, prose-only rewrite. Playtest 2026-06-13 (beneath_sunden): a missed
opponent reprisal surfaced "The roll of 3 misses. No damage to you." as a
leading paragraph — the engine rolled a 4, proving the narrator invented the
number.
"""

from __future__ import annotations

import re

# Tool names whose presence in a turn's tool-call ledger means a real die was
# rolled THIS turn — a number the narrator may legitimately reference. Today
# only the narrator-facing ``roll_dice`` tool produces a narrator-visible roll.
ROLL_TOOL_NAMES: frozenset[str] = frozenset({"roll_dice"})

# Each pattern is an unambiguous mechanical-citation tell. They require a DIGIT
# (or a bare die/AC token) so ordinary fiction — "he rolled to the side", "she
# rolled her eyes" — never trips the guard. Case-insensitive.
_FABRICATED_ROLL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bd20\b", re.IGNORECASE),
    re.compile(r"\broll\s+of\s+\d+", re.IGNORECASE),
    re.compile(
        r"\brolled?\s+(?:a\s+|an\s+)?\d+\s+(?:on|to[\s-]?hit|against|vs\.?|and)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou\s+roll(?:ed)?\s+(?:a\s+|an\s+)?\d+", re.IGNORECASE),
    re.compile(r"\bvs\.?\s*ac\b", re.IGNORECASE),
    re.compile(r"\bac\s+\d+\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+to[\s-]?hit\b", re.IGNORECASE),
    re.compile(r"\bnat(?:ural)?\s+(?:20|1)\b", re.IGNORECASE),
)


# System prompt for the prose-only rewrite pass. Toolless (tools=[]) so it
# cannot mutate or double-apply game state — it only launders the prose. The
# fiction is authoritative; only the invented mechanic is stripped.
FABRICATED_ROLL_REWRITE_SYSTEM = (
    "You are a copy editor for a tabletop RPG narrator. The narration below "
    "leaked an INVENTED dice/roll/AC number — the narrator was never given any "
    "die result, so any 'rolled N', 'roll of N', 'd20', 'vs AC', 'AC N', or "
    "'N to hit' phrasing is a fabricated mechanic and must be removed.\n\n"
    "Rewrite the narration so it tells the SAME fictional outcome (who acted, "
    "whether the blow landed or missed, the mood) using ONLY in-world prose. "
    "Strip every dice/roll/AC/number-mechanic reference and any stray separators "
    "('---'), empty leading lines, or mechanical-summary sentences. Do not add "
    "new events, do not change who hit or missed, do not invent damage. Return "
    "ONLY the cleaned narration prose — no preamble, no commentary."
)


def detect_fabricated_roll(narration: str, *, roll_tool_fired: bool) -> str | None:
    """Return the first fabricated-roll substring in ``narration``, or None.

    ``roll_tool_fired`` short-circuits to None: when a dice tool produced a real
    number this turn, a roll reference may be legitimate, so the guard stands
    down rather than risk a false positive on an honest dice turn. The leaked
    cases this guard exists for (server-side reprisals, opposed NPC checks) fire
    no narrator dice tool, so ``roll_tool_fired`` is False for them.
    """
    if roll_tool_fired:
        return None
    if not narration:
        return None
    for pattern in _FABRICATED_ROLL_PATTERNS:
        match = pattern.search(narration)
        if match is not None:
            return match.group(0)
    return None
