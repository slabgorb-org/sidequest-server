"""Story 158-38 Facet 1 (RED) — verb-agreement residuals in the POV localizer.

Follow-up to DONE 158-8 / 158-14. Two verb-agreement misses survived the
name->you swap and surfaced in later playtests. Both are pure
``swap_to_second_person`` string-transform defects, so they are pinned here at
the unit level against the real localizer (no harness needed).

FINDING 1 — bare ``then <verb>`` coordination (pingpong 2026-06-23, MP
beneath_sunden). Passes 8 and 9 re-conjugate a coordinated verb after ``and``
and after ``,`` (incl. an ``and then`` / ``, then`` adverb-skip), but a verb
coordinated by a BARE ``then`` (no preceding comma or ``and``) is stranded:

    "Carl checks the anchor then grips the rope and swings."
      currently -> "You check the anchor then grips the rope and swing."  (BUG)
      want      -> "You check the anchor then grip  the rope and swing."

FINDING 2 — subject-auxiliary inversion (pingpong 2026-06-23). A 3rd-person
auxiliary that sits IMMEDIATELY BEFORE the swapped subject (interrogative
inversion) is never re-agreed, because every existing pass conjugates the verb
AFTER the name, never before it:

    "Which way does Carl mean to go?"
      currently -> "Which way does you mean to go?"  (BUG)
      want      -> "Which way do  you mean to go?"

The guards in each section pin the existing (correct) behavior the fix must NOT
regress: the clause-local ``had_subject_swap`` gate, the verb heuristic that
leaves articles/NPC-governed verbs alone, and the already-working
``and``/``,`` coordinations.
"""

from __future__ import annotations

import pytest

from sidequest.agents.pov_swap import swap_to_second_person

# ---------------------------------------------------------------------------
# Finding 1 — bare "then <verb>" coordination
# ---------------------------------------------------------------------------


def test_bare_then_coordinated_verb_is_conjugated_full_repro() -> None:
    """The exact 2026-06-23 repro string. 'then grips' must become 'then grip'
    while the already-handled 'and swings' -> 'and swing' keeps working."""
    text = "Carl checks the anchor then grips the rope and swings."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You check the anchor then grip the rope and swing.", repr(out)


def test_bare_then_minimal_pair() -> None:
    """Smallest case: 'Carl steps then turns.' -> 'You step then turn.'"""
    text = "Carl steps then turns."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You step then turn.", repr(out)
    assert "then turns" not in out


def test_bare_then_she_her_variant() -> None:
    """The bare-then fix is pronoun-agnostic — exercise she/her too."""
    text = "Vesna lifts the latch then pulls the door wide."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns="she/her")
    assert out == "You lift the latch then pull the door wide.", repr(out)


# --- Finding 1 guards: the bare-then pass must stay clause-local + verb-only ---


def test_bare_then_does_not_conjugate_noun_governed_verb() -> None:
    """A verb whose subject is a following noun phrase (not 'you') must stay
    3rd-person: 'then the gate slams' belongs to 'the gate', not the PC."""
    text = "Carl steps back then the gate slams."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "then the gate slams" in out, repr(out)
    assert out == "You step back then the gate slams.", repr(out)


def test_bare_then_is_clause_local_and_does_not_bleed_to_npc() -> None:
    """A bare-then in a ';'-clause that never named the PC heads an NPC
    referent and must NOT be conjugated (mirrors the Pass 8/9 153-29 gate)."""
    text = "Carl nods; the guard frowns then leaves."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out.startswith("You nod;"), repr(out)
    assert "the guard frowns then leaves" in out, repr(out)


def test_existing_and_then_coordination_still_works() -> None:
    """Non-regression: the Pass 8 'and then <verb>' adverb-skip is untouched."""
    text = "Carl raises the lantern and then peers inside."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You raise the lantern and then peer inside.", repr(out)


def test_existing_comma_then_coordination_still_works() -> None:
    """Non-regression: the Pass 9 ', then <verb>' adverb-skip is untouched."""
    text = "Carl steadies the pistol, then fires."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You steady the pistol, then fire.", repr(out)


# ---------------------------------------------------------------------------
# Finding 2 — subject-auxiliary inversion ("does you" -> "do you")
# ---------------------------------------------------------------------------


def test_inverted_auxiliary_does_you_full_repro() -> None:
    """The exact 2026-06-23 repro: a question's leading 'does' before the
    swapped 'you' must agree to 'do'."""
    text = "Which way does Carl mean to go?"
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "do you mean" in out, repr(out)
    assert "does you" not in out, repr(out)


@pytest.mark.parametrize(
    ("text", "want_fragment", "bug_fragment"),
    [
        ("Which way does Carl mean to go?", "do you mean", "does you"),
        ("Where has Carl gone?", "have you gone", "has you"),
        ("How long was Carl waiting?", "were you waiting", "was you"),
    ],
)
def test_inverted_auxiliary_mid_sentence_agrees(
    text: str, want_fragment: str, bug_fragment: str
) -> None:
    """A 3rd-person auxiliary immediately preceding the swapped 'you'
    (interrogative inversion) agrees to its 2nd-person form."""
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert want_fragment in out, repr(out)
    assert bug_fragment not in out, repr(out)


def test_inverted_auxiliary_sentence_initial_capitalizes() -> None:
    """A sentence-initial inverted auxiliary keeps its capital after agreement:
    'Is Carl ready?' -> 'Are you ready?'."""
    text = "Is Carl ready?"
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "Are you ready" in out, repr(out)
    assert "Is you" not in out, repr(out)


# --- Finding 2 guard: only the auxiliary governing the swapped 'you' agrees ---


def test_inverted_auxiliary_does_not_overreach_to_other_subject() -> None:
    """A 'does' that governs a non-PC subject ('the gate') must stay 3rd-person
    even though the same clause swapped the PC elsewhere."""
    text = "Carl watches as the gate does its work."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "the gate does its work" in out, repr(out)
    assert out == "You watch as the gate does its work.", repr(out)
