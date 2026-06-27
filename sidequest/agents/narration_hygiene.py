"""Narration hygiene — strip leaked meta-cognitive preambles from player prose.

Playtest 2026-06-20 (sq-playtest-pingpong) [BUG / WWN-COMBAT-NARRATOR-LEAK]:
the WWN combat narrator leaked its mechanics->prose transition verbatim into the
rendered NarrationCard —

    "The blade finds its mark - 6 damage, a clean hit. Now I narrate.

     Kantos drives the iron home, ..."

The leading scratchpad ("6 damage, a clean hit") and the meta-cognitive
transition ("Now I narrate.") are out-of-fiction machinery the player must
never see. The narrator occasionally verbalizes the act of narrating; when it
does, that self-reference anchors a leading preamble that carries whatever raw
reasoning preceded it.

This module is the post-narration STRIP safety net (the sibling
``narrator_directives.py`` is the prompt-side PREVENTION of the 2026-06-19
``must_not_narrate`` leak). It strips the leading preamble up to and including
the narrator's self-referential "...narrate" marker, and emits a
``narrator.meta_preamble_stripped`` OTEL span every turn so the GM panel can see
the hygiene pass fire — ``stripped=False`` is the steady state, like
``narrator.canonical_leak_audit``.

Scope (deliberate, per the board): this strips the META-COGNITIVE PREAMBLE leak
only. The unbacked-damage half of the same finding (the narrator improvising "6
damage" because the WN round engine never resolved it) is epic-108 / ADR-143 —
out of scope here. The raw damage scratchpad is removed only as collateral when
it sits inside the stripped preamble; bare stats with no meta-marker stay the
province of ``combat_rules.md`` ("not 'You take 4 damage'").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opentelemetry import trace

META_PREAMBLE_STRIPPED_SPAN = "narrator.meta_preamble_stripped"

_tracer = trace.get_tracer("sidequest.narration_hygiene")

# A leaked preamble is always at the START of the prose; a "...narrate"
# self-reference deeper than this window is treated as in-fiction text (a bard
# offering to narrate a tale), NOT machinery — stripping from index 0 to a deep
# marker would nuke real prose. Generous: a real scratchpad preamble is one
# short line.
_LEADING_WINDOW = 400

_SENTENCE_END = frozenset(".!?\n")

# The narrator referring, in the first person, to its own act of narrating — the
# out-of-fiction tell. Anchors the strip. Covers the transition-announcement
# form ("Now I narrate", "Now I'll narrate", "let me narrate", "time to
# narrate", "narrating now") and the break-frame refusal form ("I cannot
# narrate ...", the 2026-06-19 leak class). The first-person subject is required
# so a CHARACTER's "...offers to narrate the tale" (subject = the bard) does not
# match. Apostrophe class covers straight + curly.
_META_NARRATION_MARKER = re.compile(
    r"(?i)\b(?:"
    r"now,?\s+i(?:['’]?ll|\s+will|\s+shall)?\s+narrate"
    r"|(?:let me|let['’]?s|time to|i['’]?m\s+going\s+to|i\s+shall|i['’]?ll|i\s+will)\s+narrate"
    # sq-playtest 2026-06-27 (GM-NOTE leak): the model signed off its scratchpad
    # with "I just need to narrate it" / "I need to narrate" — a first-person
    # reference to the act of narrating that the earlier alternatives missed.
    r"|i\s+(?:just\s+)?need\s+to\s+narrate"
    r"|i\s+(?:cannot|can['’]?t|won['’]?t|will\s+not|must\s+not)\s+narrate"
    r"|narrating\s+now"
    r")\b"
)

# sq-playtest 2026-06-27 (beneath_sunden Harpo) [GM-NOTE-LEAK]: a SECOND leak
# form — the model emits its raw chain-of-thought as a leading block, a Markdown
# horizontal rule, then the prose. The scratchpad always carries an unmistakable
# out-of-fiction tell: the internal "GM-NOTE", an internal tool name (``wn_attack``
# and the other WRITE/resolution tools), or a first-person "I (don't) need to
# call/narrate". These NEVER appear in genre-true fiction, so they safely gate the
# rule-based strip below (a bare scene-break ``---`` with no tell stays untouched).
_META_REASONING_TELL = re.compile(
    r"(?i)(?:"
    r"\bGM-?NOTE\b"
    r"|`?\bwn_[a-z_]+\b`?"  # internal WN tool names: wn_attack, wn_round, ...
    r"|\b(?:apply_damage|apply_status|advance_confrontation|advance_encounter_beat"
    r"|roll_dice|begin_confrontation|beat_selections?)\b"
    r"|\bI\s+(?:don['’]?t\s+|do\s+not\s+)?need\s+to\s+(?:call|narrate)\b"
    r")"
)

# A Markdown horizontal rule on its own line — the separator the model puts
# between its scratchpad and the prose. ``-{3,}`` is ASCII hyphens only, so a
# unicode em-dash inside the prose never matches.
_HORIZONTAL_RULE = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*$")

# The reasoning block can be verbose, so the rule-form window is more generous
# than the marker-form window — the strong out-of-fiction tell (not the window)
# is what prevents a false positive.
_REASONING_BLOCK_WINDOW = 800


@dataclass(frozen=True)
class MetaPreambleScrub:
    """Result of a meta-preamble scrub.

    Attributes:
        cleaned: Player-safe prose. Equals the input when nothing was stripped.
        stripped: True when a leading meta-preamble was removed AND real prose
            survives. Never True when stripping would blank the narration.
        fragment: The removed preamble (stripped of surrounding whitespace), or
            ``""`` when nothing was stripped. Kept for GM-panel forensics.
    """

    cleaned: str
    stripped: bool
    fragment: str


def _strip(prose: str) -> tuple[str, bool, str, bool]:
    """Pure strip core. Returns (cleaned, stripped, fragment, marker_found)."""
    if not prose:
        return prose, False, "", False

    # Form 1 (sq-playtest 2026-06-27): scratchpad ``---`` prose. A horizontal
    # rule whose preceding block carries an out-of-fiction tell (GM-NOTE /
    # internal tool name / "I need to call|narrate"). Checked first because it is
    # the more specific, structural signature; the marker form is the fallback.
    rule = _HORIZONTAL_RULE.search(prose)
    if rule is not None and rule.start() <= _REASONING_BLOCK_WINDOW:
        head = prose[: rule.start()]
        if _META_REASONING_TELL.search(head):
            remainder = prose[rule.end() :].lstrip()
            if remainder:
                # Whatever scratchpad preceded the rule (GM-NOTE, tool names,
                # the engine's pre-resolution) goes with it.
                return remainder, True, prose[: rule.end()].strip(), True
            # Nothing real follows the rule — failsafe, never blank the card.
            return prose, False, "", True

    match = _META_NARRATION_MARKER.search(prose)
    if match is None:
        return prose, False, "", False

    # Only a LEADING preamble is a leak — a deep marker is left untouched.
    if match.start() > _LEADING_WINDOW:
        return prose, False, "", True

    # The preamble runs from index 0 through the sentence that contains the
    # marker — so whatever raw scratchpad preceded the marker ("6 damage, a
    # clean hit.") is removed with it.
    term_idx: int | None = None
    for i in range(match.end(), len(prose)):
        if prose[i] in _SENTENCE_END:
            term_idx = i
            break

    if term_idx is None:
        # Marker runs to end-of-string: the whole content is preamble. Failsafe
        # — never blank the narration.
        return prose, False, "", True

    preamble = prose[: term_idx + 1]
    remainder = prose[term_idx + 1 :].lstrip()
    if not remainder:
        # Nothing real follows the preamble — failsafe, keep the original.
        return prose, False, "", True

    return remainder, True, preamble.strip(), True


def scrub_meta_preamble(prose: str) -> MetaPreambleScrub:
    """Strip a leaked meta-cognitive preamble from player-facing narration.

    Emits the ``narrator.meta_preamble_stripped`` span as a side effect (every
    call, ``stripped=False`` in steady state) so the GM panel can confirm the
    hygiene pass ran. Pure aside from the span; no other I/O.
    """
    cleaned, stripped, fragment, marker_found = _strip(prose)

    with _tracer.start_as_current_span(META_PREAMBLE_STRIPPED_SPAN) as span:
        span.set_attribute("marker_found", marker_found)
        span.set_attribute("stripped", stripped)
        span.set_attribute("fragment", fragment[:200])
        span.set_attribute("fragment_len", len(fragment))

    return MetaPreambleScrub(cleaned=cleaned, stripped=stripped, fragment=fragment)


__all__ = ["META_PREAMBLE_STRIPPED_SPAN", "MetaPreambleScrub", "scrub_meta_preamble"]
