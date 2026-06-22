"""Unit tests for the 2nd-person POV swap helper (Story 49-8).

Found in the 2026-05-12 Carl/Donut/Katia caverns_sunden playtest: every
multi-card round all three connected tabs received every per-PC POV
narration card in third-person, identically. On Carl's tab, Carl's
own action card should read "You plant a boot..." instead of "Carl
plants a boot..."

The swap helper rewrites third-person references to a single named
target into second-person, using the target's pronouns to pick the
right verb conjugation and possessive forms. Pure string transform —
no network, no LLM. Lives at ``sidequest.agents.pov_swap``.

Contract (target_name, pronouns "he/him" | "she/her" | "they/them"):
    "Carl plants a boot on the moth's thorax"
      target="Carl", pronouns="he/him"
        -> "You plant a boot on the moth's thorax"

    "Donut's mace arrives a beat behind"
      target="Donut", pronouns="he/him"
        -> "Your mace arrives a beat behind"

The helper returns ``(rewritten_text, swap_count)`` so the caller can
record an OTEL span attribute ``swap_count`` for the GM panel.

These tests RED until the helper module exists. They prove:
    1. The basic name -> "You" substitution at sentence start.
    2. Possessive: "Carl's mace" -> "Your mace".
    3. Reflexive: "Carl ducks behind himself" -> "You duck behind yourself".
    4. Mid-sentence: "the bolt grazes Carl's shoulder" -> "the bolt
       grazes your shoulder".
    5. Verb conjugation: 3rd-person s drops ("plants" -> "plant",
       "watches" -> "watch", "hauls" -> "haul").
    6. Pronoun-driven conjugation for they/them: target keeps plural
       verb ("Sam ducks" with they/them: this is the singular-they
       conjugation issue — see test for the exact rule).
    7. Dialogue protection: text inside double quotes is NOT swapped.
    8. Empty target name: helper raises (fail-loud, not a silent no-op).
    9. swap_count reflects the actual number of substitutions.
"""

from __future__ import annotations

import re

import pytest

# RED until sidequest.agents.pov_swap is created. The import must be at
# module scope so collection itself fails until Dev implements the module —
# a strong RED signal in the runner output.
from sidequest.agents.pov_swap import swap_to_second_person

# ---------------------------------------------------------------------------
# Basic substitution
# ---------------------------------------------------------------------------


def test_name_at_sentence_start_swaps_to_you_he_him():
    text = "Carl plants a boot on the moth's thorax and hauls the polearm out wet."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You plant a boot on the moth's thorax and haul the polearm out wet."
    assert count >= 1, "subject swap counts as at least one substitution"


def test_name_at_sentence_start_swaps_to_you_she_her():
    text = "Katia eases the knife back out by quarter-inches."
    out, count = swap_to_second_person(text, target_name="Katia", pronouns="she/her")
    assert out == "You ease the knife back out by quarter-inches."
    assert count >= 1


def test_name_at_sentence_start_swaps_to_you_they_them():
    """Singular-they target: "Sam ducks" -> "You duck" (plural verb form
    after 'you', regardless of whether they/them is singular or plural).
    This is the standard English convention for 2nd-person 'you'."""
    text = "Sam ducks behind the pillar."
    out, count = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert out == "You duck behind the pillar."
    assert count >= 1


# ---------------------------------------------------------------------------
# Possessive
# ---------------------------------------------------------------------------


def test_possessive_swaps_to_your():
    text = "Donut's mace arrives a beat behind."
    out, count = swap_to_second_person(text, target_name="Donut", pronouns="he/him")
    assert out == "Your mace arrives a beat behind."
    assert count >= 1


def test_possessive_mid_sentence_swaps():
    text = "The bolt grazes Carl's shoulder before clattering off the wall."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "The bolt grazes your shoulder before clattering off the wall."
    assert count >= 1


def test_possessive_after_paragraph_break_capitalizes():
    """Regression: 2026-05-18 MP playtest. Narrator returns prose with a
    paragraph break, and the new paragraph's first sentence starts with
    a possessive name. The leading capital must be preserved after the
    swap. Before the fix, `\\n\\n` did not satisfy the `.!?` check in
    `_is_sentence_start_in`, so "Laverne's hand" became lowercase
    "your hand"."""
    text = (
        "The shaft yawns dark as a swallowed bell —\n\n"
        "Laverne's hand finds the rope, cold and slick with shaft-damp."
    )
    out, _ = swap_to_second_person(text, target_name="Laverne", pronouns="she/her")
    assert "Your hand finds the rope" in out
    assert "your hand finds the rope" not in out


def test_possessive_after_em_dash_break_capitalizes():
    """Paragraph break preceded by an em-dash (a common narrator beat
    closer) must still be recognized as a sentence start."""
    text = "Old timber groans —\n\nCarl's grip tightens on the handle."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "Your grip tightens" in out
    assert "your grip tightens" not in out


# ---------------------------------------------------------------------------
# Reflexive
# ---------------------------------------------------------------------------


def test_reflexive_himself_swaps_to_yourself():
    text = "Carl shoulders himself between the moth and the door."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "yourself" in out
    assert "himself" not in out


def test_reflexive_herself_swaps_to_yourself():
    text = "Katia braces herself against the slab."
    out, _ = swap_to_second_person(text, target_name="Katia", pronouns="she/her")
    assert "yourself" in out
    assert "herself" not in out


def test_reflexive_themself_or_themselves_swaps_to_yourself():
    """Singular-they reflexive can be 'themself' or 'themselves' — both
    surface in the playgroup's chargen since the narrator follows whatever
    flavor reads in the prose. Either form must swap."""
    text = "Sam steadies themself on the railing."
    out, _ = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert "yourself" in out
    assert "themself" not in out and "themselves" not in out


# ---------------------------------------------------------------------------
# Pronoun substitution — third-person -> second-person
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Comma-coordinated verb continuation (sq-playtest 2026-05-15)
# ---------------------------------------------------------------------------


def test_comma_coordinated_verb_continuation_conjugates():
    """The original playtest repro: a single sentence chains three actions
    through commas + and. Pass 2 caught "thumbs", Pass 8 caught "works",
    but the middle "sets" stayed in 3rd-person form. Result on the actor's
    own tab read: "you thumb..., sets..., and work..." — mixed conjugation.

    After the fix, all three verbs are second-person.
    """
    text = (
        "Willes thumbs open the component pouch's outer flap for the chalk-stub, "
        "sets the bronze fitting in your eye-line at three paces, and works the "
        "curl of it onto a corner of waxed parchment."
    )
    out, _ = swap_to_second_person(text, target_name="Willes", pronouns="he/him")
    # All three verbs should be in 2nd-person form. "You" is sentence-start
    # here (the sentence begins "Willes thumbs..." → "You thumb...").
    assert out.startswith("You thumb open"), out
    assert ", set the bronze fitting" in out, out
    assert "and work the curl" in out, out
    # And no 3rd-person residue.
    assert "thumbs" not in out
    assert "sets" not in out
    assert "works the" not in out


def test_comma_coordinated_continuation_skips_non_verbs():
    """Conservative gating: a comma followed by a non-verb (article,
    preposition, plural noun) must NOT be conjugated. Otherwise an
    appositive or parenthetical commas would get mangled.
    """
    text = "Carl plants a boot on the moth's thorax, the polearm braced against his shoulder."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    # "the" doesn't end in -s; pass-through.
    assert ", the polearm" in out, out
    # And the substantive swap still works.
    assert out.startswith("You plant a boot"), out


def test_comma_coordinated_continuation_only_when_subject_was_swapped():
    """The pass is gated by ``had_subject_swap`` so a sentence that didn't
    mention the target at all is left entirely alone. Defensive: we never
    conjugate commas in sentences belonging to other actors.
    """
    text = "Alice waves, gestures at the door, and beckons Bob inside."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    # No mention of Carl → no swap, no conjugation, no mutation.
    assert out == text


def test_comma_continuation_with_she_her():
    """Pronoun-set parity check: the comma fix must work for she/her too."""
    text = "Katia draws the knife, twists the blade, and steps back."
    out, _ = swap_to_second_person(text, target_name="Katia", pronouns="she/her")
    assert out == "You draw the knife, twist the blade, and step back."


# ---------------------------------------------------------------------------
# Subject-verb interrupter (sq-playtest 2026-05-17 / [BS-BUG-LOW])
# ---------------------------------------------------------------------------
#
# Beneath Sünden 3-player MP, turn 2, Carl's own card rendered:
#   "you works the drum with your hands, checking the rope…"
# Second-person actor "You", third-person verb "works". Root cause: Pass 2
# only conjugates the single token immediately after the name. When an
# adverb / appositive / parenthetical sits between the subject and its
# verb, that interrupter is captured instead of the verb, and the real
# main verb is never visited by any conjugating pass. Pronoun-agnostic
# (reproduces for he/him, she/her, they/them alike).


def test_adverb_between_subject_and_verb_conjugates():
    """The verbatim playtest repro: an adverb strands the main verb.
    "Carl steadily works the drum with his hands, checking the rope."
    must become "You steadily work the drum with your hands, checking the
    rope." — not "You steadily works…".

    UPDATED by Story 153-29 (was "…with his hands…" under the retired-
    pronoun-passes contract): the possessive "his" now AGREES to "your"
    because this clause already had a name-driven swap of the PC (the
    antecedent gate). The primary assertion this test exists for — the
    adverb-stranded main verb conjugates ("works" -> "work") — is unchanged.
    """
    text = "Carl steadily works the drum with his hands, checking the rope."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You steadily work the drum with your hands, checking the rope.", out
    assert "works" not in out


def test_appositive_between_subject_and_verb_conjugates():
    """A comma appositive blocks Pass 2 (the comma defeats ``\\s+`` after
    the name) and falls to the bare-name pass. The main verb after the
    appositive must still be conjugated."""
    text = "Carl, crouched at the rim, works the drum."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You, crouched at the rim, work the drum.", out
    assert "works" not in out


def test_parenthetical_between_subject_and_verb_conjugates():
    """An em-dash parenthetical interrupter strands the verb the same way."""
    text = "Carl — still braced — works the drum."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You — still braced — work the drum.", out
    assert "works" not in out


def test_interrupter_conjugation_they_them_parity():
    """The defect is pronoun-set-agnostic — it must also be fixed for a
    they/them PC (the playgroup's Katia profile).

    UPDATED by Story 153-29 (was "…with their hands…" under the retired-
    pronoun-passes contract): the possessive "their" now AGREES to "your"
    via the antecedent-gated pronoun pass. The primary assertion — the
    interrupter main verb conjugates ("works" -> "work") — is unchanged."""
    text = "Sam carefully works the drum with their hands."
    out, _ = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert out == "You carefully work the drum with your hands.", out
    assert "works" not in out


def test_interrupter_pass_does_not_fire_for_other_actors():
    """Defensive: a sentence with an interrupter that does NOT mention the
    target is returned entirely unchanged — the new forward-scan must not
    conjugate verbs in prose belonging to peers."""
    text = "Donut, crouched at the rim, works the drum."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == text
    assert count == 0


# ---------------------------------------------------------------------------
# Antecedent-blindness retired (sq-playtest 2026-05-23 pulp_noir/annees_folles)
# ---------------------------------------------------------------------------
#
# The original Story 49-8 helper applied PRONOUN-LEVEL substitutions (he → you,
# his → your, him → you, himself → yourself) anywhere those tokens appeared in
# the anchored prose. Regex has no antecedent resolution: in a scene with an
# NPC who shares the PC's pronouns ("the man with Le Figaro folds his paper…
# He doesn't hurry."), every he/his/him/himself in the prose was rewritten —
# turning NPC actions into PC actions on the player's tab.
#
# The new contract retires the antecedent-blind pronoun passes and shifts the
# 2nd-person voice contract to the narrator side: the narrator is instructed
# (via narrator_prompts/pov_rules.md) to write the anchor PC's actions using
# the PC's NAME, never a pronoun. The reflexive ``himself``/``herself`` pass
# stays in place but is GATED on a sentence-local name swap, so it only fires
# when this sentence already contains the PC's name as the subject.
#
# The tests below pin the new contract: NPC pronouns survive untouched even
# when the PC shares their pronoun set.


def test_npc_he_with_he_him_pc_not_rewritten():
    """Verbatim sq-playtest 2026-05-23 repro: pulp_noir / annees_folles, PC
    Paul Lautrec (he/him). The narrator writes about an NPC ("the man with
    Le Figaro") leaving the café; the legacy helper rewrote "He doesn't
    hurry" to "You doesn't hurry" because Pass 5 fired on any "He" in the
    prose. After the retire, NPC pronouns survive — the only swaps come
    from the PC's actual name appearances (zero in this fragment).
    """
    text = (
        "Across the room, the man with Le Figaro folds his paper, sets a coin "
        "on the table, and walks out without looking at you. He doesn't hurry."
    )
    out, count = swap_to_second_person(text, target_name="Paul Lautrec", pronouns="he/him")
    # The PC's name doesn't appear in the prose — there is nothing to swap.
    assert out == text, out
    assert count == 0


def test_npc_she_with_she_her_pc_not_rewritten():
    """She/her parity: an NPC sister scene with a she/her PC must leave the
    NPC's pronouns untouched."""
    text = "The widow turns away. She does not look back. Her veil catches in the door."
    out, count = swap_to_second_person(text, target_name="Mme. Beaumont", pronouns="she/her")
    assert out == text
    assert count == 0


def test_npc_them_with_they_them_pc_not_rewritten():
    """They/them parity: pronoun passes are retired across all pronoun sets."""
    text = "The strangers exchange a glance. They wait. Their hands stay in their coats."
    out, count = swap_to_second_person(text, target_name="Avery", pronouns="they/them")
    assert out == text
    assert count == 0


def test_subject_pronoun_after_name_swap_in_same_clause_becomes_you():
    """UPDATED by Story 153-29 (was test_pronoun_in_predicate_after_name_swap_
    stays_third_person under the retired-pass 49-8 contract).

    A subject pronoun that co-refers with the just-swapped PC in the SAME
    clause now agrees, and its verb conjugates: 'and he hauls' → 'and you
    haul'. This is the canonical AC-2 case from the finding — person-
    disagreement inside the localized player's own tab is the bug. The
    antecedent gate (the sentence already had a name-driven swap of the
    target PC) is what makes this safe; a same-pronoun NPC sentence with no
    PC name is still left untouched (see the NPC-bleed guards above)."""
    text = "Carl plants a boot and he hauls the polearm out wet."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You plant a boot and you haul the polearm out wet.", repr(out)
    assert "he hauls" not in out


def test_object_pronoun_him_for_npc_stays_third_person():
    """CLAUSE-LOCAL GATE (Story 153-29): the object pronoun 'him' sits in a
    DIFFERENT clause (after the ';', subject 'the moth') than the name swap
    ('Carl' in the first clause). 153-29 re-introduces object-pronoun
    agreement, but gated per-clause so it does NOT cross the ';' into a clause
    about another actor — 'him' refers to the moth (or is ambiguous) and must
    stay third-person. Crossing the boundary would re-open the 2026-05-23
    NPC-bleed bug inside one engine 'sentence' (split only on .!?). The same-
    clause cases (e.g. 'Carl charges in and the blast hurls him back') DO
    convert — see the AC-3 tests below."""
    text = "Carl plants a boot; the moth shudders against him."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You plant a boot; the moth shudders against him.", out


def test_object_pronoun_her_for_npc_stays_third_person():
    """She/her parity for the clause-local gate: 'her' is in a separate
    semicolon clause ('the cold seeps') and stays third-person — the gate does
    not cross the ';'."""
    text = "Katia eases the knife; the cold seeps into her."
    out, _ = swap_to_second_person(text, target_name="Katia", pronouns="she/her")
    assert out == "You ease the knife; the cold seeps into her.", out


# ---------------------------------------------------------------------------
# Dialogue protection — text inside quotes is left alone
# ---------------------------------------------------------------------------


def test_dialogue_protected_carl_in_speech_not_swapped():
    """If another character speaks the target's name aloud, the spoken
    name is part of in-world dialogue and must NOT be rewritten — the
    speaker is referring to Carl by name, not narrating from Carl's POV.
    """
    text = 'Donut grunts, "Carl, watch the flank." Carl plants a boot.'
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    # The dialogue name stays third-person; the narrator-voice line swaps.
    assert '"Carl, watch the flank."' in out
    assert "You plant a boot." in out


def test_dialogue_protected_pronoun_in_speech_not_swapped():
    """Dialogue protection: text inside quotes is left alone by every pass.
    The narrator-voice sentence after the dialogue is swapped via the NAME
    (not the pronoun) under the new contract — well-formed prose uses the
    PC's name here, not 'She'."""
    text = 'Katia hisses, "She drew first, you know." Katia raises the knife.'
    out, _ = swap_to_second_person(text, target_name="Katia", pronouns="she/her")
    # Dialogue's 'She drew first' refers to someone else and is in quotes; stays.
    assert '"She drew first, you know."' in out
    # The narrator-voice line uses the name — swaps via Pass 2.
    assert "You raise the knife." in out


# ---------------------------------------------------------------------------
# Verb conjugation — drop 3rd-person -s, handle special verb endings
# ---------------------------------------------------------------------------


def test_verb_drops_simple_s():
    text = "Carl plants the polearm."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You plant the polearm."


def test_verb_drops_es_for_sibilant_endings():
    """Verbs ending in -ses / -shes / -ches / -xes / -zes drop -es to
    become plural form. 'watches' -> 'watch', 'hauls' -> 'haul'."""
    text = "Carl watches the door."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You watch the door."


def test_verb_drops_ies_for_consonant_y_endings():
    """'tries' -> 'try', 'flies' -> 'fly'. The -ies suffix becomes -y."""
    text = "Carl tries the lock again."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You try the lock again."


def test_irregular_verb_has_swaps_to_have():
    """'has' -> 'have'. This is high-frequency in narration ('Carl has the
    advantage')."""
    text = "Carl has the advantage."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You have the advantage."


def test_irregular_verb_is_swaps_to_are():
    """'is' -> 'are'. Equally high-frequency."""
    text = "Carl is mid-swing when the moth pivots."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You are mid-swing when the moth pivots."


def test_irregular_verb_was_swaps_to_were():
    """'was' -> 'were'. Past-tense narration uses this constantly."""
    text = "Carl was waiting for the opening."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You were waiting for the opening."


# ---------------------------------------------------------------------------
# Contracted verbs (sq-playtest 2026-05-23 pulp_noir/annees_folles, B2b)
# ---------------------------------------------------------------------------
#
# The verb-capture regex uses ``\w+`` which stops at the apostrophe — so
# "Carl doesn't move" was captured as "Carl doesn" and conjugated to "You
# doesn", leaving "'t move" in place: "You doesn't move." The fix routes
# the bare contraction stem ("doesn", "wasn", "isn", "hasn") through
# ``_IRREGULAR_VERBS`` so the conjugated stem composes correctly with the
# trailing "n't" suffix that survives outside the regex match.


def test_contraction_doesnt_after_name_conjugates_to_dont():
    """'Carl doesn't move' -> 'You don't move'. Pre-fix this rendered as
    'You doesn't move' on the player's tab."""
    text = "Carl doesn't move."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You don't move."


def test_contraction_isnt_after_name_conjugates_to_arent():
    text = "Carl isn't ready."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You aren't ready."


def test_contraction_wasnt_after_name_conjugates_to_werent():
    text = "Carl wasn't expecting that."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You weren't expecting that."


def test_contraction_hasnt_after_name_conjugates_to_havent():
    text = "Carl hasn't moved."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You haven't moved."


def test_contraction_in_and_continuation_conjugates():
    """Pass 8 ('and <verb>' continuation) must also conjugate contractions
    once the sentence's subject was swapped via a name."""
    text = "Carl plants a boot and doesn't move."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You plant a boot and don't move."


# ---------------------------------------------------------------------------
# Negative cases — fail-loud guards
# ---------------------------------------------------------------------------


def test_empty_target_name_raises():
    """Silent no-op on empty target_name would mask a chargen bug where
    the anchor PC has no name. Fail loud per project rule."""
    with pytest.raises(ValueError):
        swap_to_second_person("Some prose.", target_name="", pronouns="he/him")


def test_unknown_pronoun_string_raises():
    """The helper must reject pronoun strings it doesn't know how to
    swap. 'it/its', 'xe/xem', empty string, or any non-canonical form
    fail loud — silently defaulting to he/him would inject wrong
    grammar into player-facing prose."""
    with pytest.raises(ValueError):
        swap_to_second_person("Carl plants a boot.", target_name="Carl", pronouns="xe/xem")


def test_target_name_absent_returns_unchanged_and_zero_count():
    """If the target name doesn't appear in the text (atmospheric prose,
    or peer's card), the helper returns the original prose and count=0.
    No exception — atmospheric narration is a valid input."""
    text = "Rain hammers the slate. The corridor narrows."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == text
    assert count == 0


# ---------------------------------------------------------------------------
# swap_count semantics
# ---------------------------------------------------------------------------


def test_swap_count_matches_substitution_total():
    """Count should reflect the total number of distinct swap operations so
    the OTEL span has a meaningful 'how much did this rewrite' signal.

    Under the new (pronoun-pass retired) contract, only name-based swaps
    fire — antecedent-blind pronoun-passes have been removed. The third
    sentence ('He shoulders himself…') has no Carl mention so nothing in
    it gets touched: 'He' / 'himself' both survive. The pov_rules.md
    narrator instruction is what keeps well-formed prose from reaching this
    fallback in the first place.
    """
    text = (
        "Carl plants a boot on the moth's thorax. "
        "Carl's polearm slides free. "
        "He shoulders himself between Donut and the door."
    )
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    # Sentence 1: Carl + plants → 2 swaps. Sentence 2: Carl's → 1. Sentence 3: 0.
    assert count >= 3, f"expected at least 3 swaps in the dense passage, got {count}"
    # The Carl name is gone (Pass 1/2 fired).
    assert "Carl" not in out
    # Sentence 3 pronouns survive — antecedent unknown to the regex layer.
    assert "He shoulders himself" in out


def test_swap_count_zero_when_no_match():
    text = "Rain falls on the slate roof."
    _, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert count == 0


# ---------------------------------------------------------------------------
# Multi-name safety — only the target swaps, peers do not
# ---------------------------------------------------------------------------


def test_only_target_swaps_other_pcs_untouched():
    """In MP narration that mentions multiple PCs, only the anchor swaps.
    Donut/Katia stay third-person on Carl's tab."""
    text = "Carl plants a boot; Donut's mace arrives a beat behind; Katia eases the knife back."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "Donut's mace" in out, "Donut must stay third-person"
    assert "Katia eases" in out, "Katia must stay third-person"
    assert "You plant a boot" in out


# ---------------------------------------------------------------------------
# Story 71-6: predicate/absolute possessive → "yours" (Bug 1)
# ---------------------------------------------------------------------------


def test_predicate_possessive_mid_sentence_becomes_yours():
    """Clause-final {Name}'s (before period) → 'yours', not 'your'."""
    text = "The polearm was Carl's."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "The polearm was yours.", repr(out)
    assert count >= 1


def test_predicate_possessive_before_comma_becomes_yours():
    """Predicate possessive before a comma → 'yours'."""
    text = "The decision was Carl's, not hers."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "was yours," in out, repr(out)


def test_predicate_possessive_at_sentence_start_capitalises():
    """Sentence-initial predicate possessive → 'Yours' (capital)."""
    text = "Carl's, that burden."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out.startswith("Yours,"), repr(out)


def test_predicate_possessive_before_coordinating_conj_becomes_yours():
    """Predicate possessive before 'and'/'but'/etc. → 'yours'."""
    text = "The choice was Carl's and he knew it."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "was yours and" in out, repr(out)


def test_attributive_possessive_still_becomes_your():
    """Regression: attributive 'Carl's polearm' → 'Your polearm', not 'Yours'."""
    text = "Carl's polearm lies across the threshold."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out.startswith("Your polearm"), repr(out)
    assert "Yours" not in out


def test_attributive_possessive_mid_sentence_stays_your():
    """Mid-sentence attributive possessive: 'Carl's sharp eye' → 'your sharp eye'."""
    text = "The crowd follows Carl's sharp eye to the door."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "your sharp eye" in out, repr(out)
    assert "yours" not in out.lower()


# ---------------------------------------------------------------------------
# Story 71-6: stranded continuation verb after connector+adverb (Bug 2)
# ---------------------------------------------------------------------------


def test_comma_then_verb_conjugated():
    """Pass 9 adverb-skip: ', then <3rd-verb>' → ', then <2nd-verb>'."""
    text = "Carl steadies the pistol, then fires."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You steady the pistol, then fire.", repr(out)
    # subject-swap (steadies→steady = 2) + comma-then-verb (fires→fire = 1) = 3
    assert count == 3


def test_and_adverb_verb_conjugated():
    """Pass 8 adverb-skip: 'and <adverb> <3rd-verb>' → 'and <adverb> <2nd-verb>'."""
    text = "Carl turns and slowly raises the lantern."
    out, count = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You turn and slowly raise the lantern.", repr(out)


def test_plural_noun_after_comma_not_conjugated():
    """Regression: ', the bronze fittings gleam.' — 'fittings' must NOT be conjugated
    by the adverb-skip (it is a plural noun, not a 3rd-person verb in this position)."""
    text = "Carl nods, the bronze fittings gleam."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "fittings gleam" in out, repr(out)
    assert "fitting gleam" not in out


def test_npc_name_after_comma_verb_not_conjugated():
    """Regression: NPC name after comma must NOT be treated as an adverb-to-skip.
    'Carl nods, Maria steps forward.' → NPC verb 'steps' must stay 3rd-person."""
    text = "Carl nods, Maria steps forward."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "Maria steps" in out, repr(out)
    assert "Maria step " not in out


def test_npc_name_after_and_verb_not_conjugated():
    """Regression: NPC name after 'and' must NOT be treated as an adverb-to-skip.
    'Carl turns and Maria calls out.' → NPC verb 'calls' must stay 3rd-person."""
    text = "Carl turns and Maria calls out."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert "Maria calls" in out, repr(out)
    assert "Maria call " not in out


# ---------------------------------------------------------------------------
# [BAR-2] Descriptive comma-clause corruption (sq-playtest 2026-06-06
# heavy_metal/barsoom, solo, "Zanzibar Jones"). After a name-driven subject
# swap, Pass 8/9's stranded-verb passes treated ANY -s word following a
# comma/"and" as a 3rd-person verb and de-conjugated it. In rich narration
# the post-comma material is overwhelmingly an absolute / appositive /
# participial phrase headed by a PLURAL NOUN or POSSESSIVE PRONOUN, not a
# coordinated verb — so "four arms"→"four arm", "ivory tusks"→"ivory tusk",
# "turning its"→"turning it", and (retro-explaining #708) "his"→"hi".
#
# The fix radically constrains the adverb-skip branch to REAL adverbs
# ("then" / -ly) and adds the missing pronoun guard the 2026-05-23 Pass
# 5/6/7 retirement implied (pronouns are never verbs and must never be
# conjugated). The legit verb-coordination cases above
# (test_comma_coordinated_verb_continuation_conjugates, test_*_adverb_verb_*)
# still pass — only the non-verb over-fire is removed.
# ---------------------------------------------------------------------------


def test_number_plural_noun_after_comma_not_conjugated():
    """', four arms loose at its sides' — 'arms' is a plural noun in an
    absolute phrase, NOT a coordinated verb. 'four' is a number, not a
    skippable adverb."""
    text = "Zanzibar Jones holds still, four arms loose at its sides."
    out, _ = swap_to_second_person(text, target_name="Zanzibar Jones", pronouns="he/him")
    assert "four arms loose" in out, repr(out)
    assert "four arm loose" not in out


def test_adjective_plural_noun_absolute_after_comma_not_conjugated():
    """', ivory tusks catching the light' — 'tusks' is a plural noun;
    'ivory' is an adjective, not a skippable adverb."""
    text = "The shape waits, ivory tusks catching the light."
    out, _ = swap_to_second_person(text, target_name="Zanzibar Jones", pronouns="he/him")
    # No subject swap happened (Zanzibar not present) → returned unchanged,
    # but this also pins that the bare descriptive clause is never touched.
    assert "ivory tusks catching" in out, repr(out)
    assert "ivory tusk catching" not in out


def test_possessive_its_after_comma_not_de_pluralized():
    """', turning its eyeless gaze' — 'its' is a possessive pronoun, never a
    verb; the stranded-verb pass must not strip it to 'it'."""
    text = "Zanzibar Jones freezes, turning its eyeless gaze across the moss."
    out, _ = swap_to_second_person(text, target_name="Zanzibar Jones", pronouns="he/him")
    assert "turning its eyeless gaze" in out, repr(out)
    assert "turning it eyeless" not in out


def test_possessive_his_after_comma_not_de_pluralized():
    """[#708 retro] ', his copper face' — the comma-continuation (Pass 9)
    must never strip the possessive 'his' to 'hi'.

    UPDATED by Story 153-29 (was asserting 'his copper face' survives): in
    this armed clause 'his' now AGREES to 'your' via the antecedent-gated
    possessive pass — NOT 'hi'. The load-bearing #708 guard (the possessive
    is never de-pluralized to 'hi') is preserved by the negative assert."""
    text = "Zanzibar Jones turns, his copper face hard."
    out, _ = swap_to_second_person(text, target_name="Zanzibar Jones", pronouns="he/him")
    assert "your copper face" in out, repr(out)
    assert "hi copper" not in out


def test_barsoom_journal_sentence_renders_clean():
    """End-to-end: the verbatim turn-2 barsoom journal sentence must render
    with zero de-pluralization corruption after the 2nd-person swap."""
    text = (
        "Zanzibar Jones presses flat against the column's cold stone, the green "
        "camp's firelight painting the broken marble amber behind him — and the "
        "great shape on the plain holds still, four arms loose at its sides, "
        "ivory tusks catching the light, turning its eyeless gaze across the moss."
    )
    out, _ = swap_to_second_person(text, target_name="Zanzibar Jones", pronouns="he/him")
    # The subject swap still fires.
    assert out.startswith("You press flat"), repr(out)
    # None of the four documented corruptions survive.
    assert "four arms loose" in out and "four arm loose" not in out, repr(out)
    assert "ivory tusks catching" in out and "ivory tusk catching" not in out, repr(out)
    assert "turning its eyeless" in out and "turning it eyeless" not in out, repr(out)


# ---------------------------------------------------------------------------
# Story 153-14: PC-name as a fragment of a longer NPC proper noun
# (NPC-NAME-PCSUBSTRING-SUBSTITUTION, sq-playtest 2026-06-20/21).
#
# When the PC's name is a substring of a multi-word NPC name, the existing
# \b...\b word boundaries are NOT enough — "Kantos Vah" has an internal word
# boundary, so \bKantos\b matched the "Kantos" token inside the NPC's full
# name and the subject pass rewrote NPC "Kantos Vah" into "you Vah" on the
# player's tab. The fix guards the name→"you" swap against firing on a token
# that sits adjacent to another capitalized word (a proper-noun continuation).
# ---------------------------------------------------------------------------


def test_pc_name_prefix_of_npc_name_left_intact():
    """Verbatim finding: PC "Kantos", NPC "Kantos Vah". The NPC's full name
    must survive untouched — never become "you Vah"."""
    text = "Kantos Vah studies the console without looking up."
    out, count = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert out == text, repr(out)
    assert count == 0


def test_pc_name_standalone_still_swaps_despite_collision_name():
    """The standalone PC reference must still swap even when an NPC whose name
    contains the PC name appears in the same passage. First sentence is the
    NPC ("Kantos Vah"); second is the PC ("Kantos") acting."""
    text = "Kantos Vah studies the console. Kantos draws the blade."
    out, _ = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert "Kantos Vah studies the console." in out, repr(out)
    assert "You draw the blade." in out, repr(out)


def test_pc_name_prefix_npc_possessive_left_intact():
    """A longer NPC name in the possessive ("Kantos Vah's blade") must not be
    partly swapped to "you Vah's blade"."""
    text = "Kantos Vah's blade gleams on the rack."
    out, count = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert out == text, repr(out)
    assert count == 0


def test_pc_possessive_still_swaps_with_collision_name():
    """Possessive regression: the PC's own possessive ("Kantos's") must still
    become "Your", even though "Kantos" is a prefix of an NPC name."""
    text = "Kantos's grip tightens on the rail."
    out, _ = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert out.startswith("Your grip tightens"), repr(out)
    assert "Kantos" not in out


def test_pc_name_suffix_of_npc_name_left_intact_mid_sentence():
    """ "Embedded in a longer NPC name" generality — the PC name as the trailing
    word of a compound proper noun mid-sentence ("the envoy Vah Kantos") is a
    name fragment and must not swap to "Vah you"."""
    text = "The envoy Vah Kantos bows to the council."
    out, count = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert out == text, repr(out)
    assert count == 0


# ===========================================================================
# Story 153-29: antecedent-gated pronoun agreement
# (MP-PRONOUN-LOCALIZATION-INCOMPLETE, sq-playtest 2026-06-20/21).
#
# The name + adjacent-verb swap (Passes 1-4) leaves possessive / subject /
# object pronouns for the SAME just-swapped PC in third person, producing
# person-disagreement inside the localized player's own tab:
#   "Vesna presses her palm"  ->  "You press her palm"   (should be "your palm")
# and, worse, a single combat sentence mixing 2nd + 3rd person for one
# character ("the weight lands on your back and something rakes across his
# shoulders").
#
# Passes 5/6/7 (subject / possessive / object pronoun) were RETIRED 2026-05-23
# because they were antecedent-blind (rewrote NPC pronouns too). Story 153-29
# re-introduces them ANTECEDENT-GATED: a pronoun is rewritten only inside the
# clause that already had a name-driven swap of the target PC. A same-pronoun
# NPC in a sentence/clause that never named the PC is left fully third-person
# (preserves the 2026-05-23 fix — AC 4).
#
# These tests are RED until the gated pronoun passes are re-introduced.
# ===========================================================================

# Any third-person pronoun (subject/object/possessive) across the three
# supported pronoun sets. AC 5 asserts NONE of these survive in a fully
# localized sentence whose pronouns all co-refer with the swapped "You".
_THIRD_PERSON_PRONOUN_RE = re.compile(
    r"\b(?:he|she|they|him|her|them|his|their|hers|theirs)\b", re.IGNORECASE
)


# --- AC 1: possessive pronoun agreement (her / his / their -> your) ---------


def test_possessive_pronoun_her_after_name_swap_becomes_your():
    """Verbatim finding: 'Vesna presses her palm flat to the gouged wall.' on
    Vesna's own tab must read 'You press your palm…', not 'You press her
    palm…'. The possessive 'her' co-refers with the swapped 'You'."""
    text = "Vesna presses her palm flat to the gouged wall."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns="she/her")
    assert out == "You press your palm flat to the gouged wall.", repr(out)
    assert "her palm" not in out


def test_possessive_pronoun_his_after_name_swap_becomes_your():
    text = "Carl raises his shield against the blow."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You raise your shield against the blow.", repr(out)
    assert "his shield" not in out


def test_possessive_pronoun_their_after_name_swap_becomes_your():
    text = "Sam grips their blade and holds the line."
    out, _ = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert out == "You grip your blade and hold the line.", repr(out)
    assert "their blade" not in out


def test_possessive_name_swap_arms_the_pronoun_gate():
    """AC 4: a name-driven POSSESSIVE swap (Pass 1) — not only a subject swap
    — must arm the pronoun gate. 'Vesna's grip tightens on her blade.' has no
    bare subject name, only the possessive 'Vesna's'; the follow-on possessive
    pronoun 'her blade' must still agree. ('grip' is the subject of 'tightens',
    not the PC, so 'tightens' stays third-person — only the two possessives
    change.)"""
    text = "Vesna's grip tightens on her blade."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns="she/her")
    assert out == "Your grip tightens on your blade.", repr(out)
    assert "her blade" not in out


# --- AC 2: follow-on subject pronoun agreement (he / she / they -> you) -----


def test_subject_pronoun_he_after_name_swap_becomes_you():
    """Finding (combat): '…before he can raise the blade' on the PC's own tab
    must read '…before you can raise the blade.' The subject pronoun 'he'
    co-refers with the swapped 'You' (same clause, no intervening NPC). 'can
    raise' is a modal phrase — no -s to drop, only the pronoun changes."""
    text = "Carl lunges before he can raise the blade."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You lunge before you can raise the blade.", repr(out)
    assert _re_word("he", out) is False


def test_subject_pronoun_she_after_name_swap_conjugates_verb():
    """She/her parity + verb agreement: 'and she strikes' -> 'and you strike'
    (the follow-on subject pronoun drops the 3rd-person -s like Pass 2)."""
    text = "Vesna steps in and she strikes."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns="she/her")
    assert out == "You step in and you strike.", repr(out)
    assert "she strikes" not in out


def test_subject_pronoun_they_after_name_swap_becomes_you():
    """Singular-they subject pronoun -> 'you'. The 'you' verb form equals the
    they form, so 'they swing' -> 'you swing' (no -s change), but the pronoun
    must still convert."""
    text = "Sam advances and they swing the hammer wide."
    out, _ = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert out == "You advance and you swing the hammer wide.", repr(out)
    assert "they swing" not in out


# --- AC 3: object pronoun agreement (him / her / them -> you) ---------------


def test_object_pronoun_him_referring_to_pc_becomes_you():
    """An object pronoun that co-refers with the swapped PC in the same clause
    agrees: 'Carl charges in and the blast hurls him back.' -> '…hurls you
    back.' ('the blast' is the subject of 'hurls', so 'hurls' stays; only the
    object 'him' converts.)"""
    text = "Carl charges in and the blast hurls him back."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You charge in and the blast hurls you back.", repr(out)
    assert "hurls him" not in out


def test_object_pronoun_her_referring_to_pc_becomes_you():
    """She/her object: 'the cold bites her' (her + terminal punctuation ==
    object form by lookahead) -> 'the cold bites you'."""
    text = "Vesna holds the line and the cold bites her."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns="she/her")
    assert out == "You hold the line and the cold bites you.", repr(out)
    assert "bites her" not in out


def test_object_pronoun_them_referring_to_pc_becomes_you():
    text = "Sam stands firm and the blow staggers them."
    out, _ = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert out == "You stand firm and the blow staggers you.", repr(out)
    assert "staggers them" not in out


# --- AC 4: antecedent gate / no NPC bleed ----------------------------------


def test_pronoun_in_separate_sentence_without_pc_name_survives():
    """The gate is per-sentence: a name swap in sentence 1 must NOT license
    pronoun rewrites in a LATER sentence that never names the PC. The verbatim
    annees_folles NPC shape, prefixed with a PC-action opener — sentence 1
    swaps, sentences 2-3 (the man / He) are untouched."""
    text = "Carl plants a boot. The man folds his paper. He doesn't hurry."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You plant a boot. The man folds his paper. He doesn't hurry.", repr(out)


def test_pronoun_converts_in_name_clause_but_not_across_semicolon_to_npc():
    """The antecedent gate is CLAUSE-local, not whole-sentence-coarse. In one
    engine 'sentence' (split only on .!?), the possessive that co-refers with
    the named PC in the FIRST clause agrees ('his shield' -> 'your shield'),
    but an object pronoun in a later ';'-delimited clause about a DIFFERENT
    subject (the troll) must stay third-person ('swings at him'). Crossing the
    clause boundary would re-open the 2026-05-23 NPC-bleed bug.

    NOTE (TEA, 153-29): the story context's gate wording is "same sentence";
    TEA pins the stricter CLAUSE-local reading to honor AC 4's stated 'no NPC
    bleed' intent. See the Design Deviation + Delivery Finding in the session
    file. If the Architect prefers the coarse same-sentence gate, this test
    (and the two ';' NPC guards above) is where that decision lands."""
    text = "Carl raises his shield; the troll swings at him."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You raise your shield; the troll swings at him.", repr(out)
    assert "your shield" in out
    assert "swings at him" in out


# --- AC 5: full person agreement on a localized multi-pronoun sentence ------


def test_localized_combat_sentence_has_no_residual_third_person_pronoun():
    """AC 5 (required): feed a localized multi-pronoun sentence whose pronouns
    all co-refer with the anchor PC (same clause, comma-chained absolutes) and
    assert FULL person agreement — no residual third-person pronoun survives.
    This is the assertion the finding demands: a single localized sentence
    must never mix 2nd + 3rd person for the same character."""
    text = "Vesna grits her teeth, pain flooding her arm, the world tilting under her."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns="she/her")
    assert out == ("You grit your teeth, pain flooding your arm, the world tilting under you."), (
        repr(out)
    )
    residual = _THIRD_PERSON_PRONOUN_RE.search(out)
    assert residual is None, (
        f"localized sentence must contain NO residual 3rd-person pronoun for the "
        f"anchor PC; found {residual.group(0)!r} in {out!r}"
    )


def _re_word(word: str, text: str) -> bool:
    """True iff ``word`` appears as a standalone token in ``text``."""
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


# ===========================================================================
# Story 153-29 — REVIEW REWORK (round-trip 1): post-semicolon clause-boundary
# regressions introduced by the `;`-clause split.
#
# The 153-29 implementation made the pronoun gate clause-local by splitting each
# engine-"sentence" on `;` and processing each clause through `_rewrite_clause`.
# That split silently changed what the position-context helpers see: a clause is
# NOT a sentence, but `_is_sentence_start_in` and `_is_proper_noun_fragment` were
# written for whole-sentence input and now treat a CLAUSE start as a SENTENCE
# start. Two behaviors that were CORRECT before the branch regressed (Reviewer,
# round-trip 1):
#
#   1. CAPITALIZATION — a PC name/possessive swapped at the start of a NON-first
#      `;`-clause must stay lowercase ("…; you step", "…; your grip"), because a
#      semicolon does not open a new sentence. The split puts a leading space on
#      the clause, so the name sits at idx 1; the new `clause_is_sentence_start`
#      param only gates the idx==0 branch, while the real path is the whitespace
#      walk-back `if j < 0: return True` — which ignores the flag. Currently the
#      swap capitalizes ("…; You step"), which is wrong mid-sentence.
#
#   2. 153-14 NPC-NAME-FRAGMENT GUARD — a multi-word NPC name whose suffix token
#      equals the PC name ("Vah Kantos", PC "Kantos") must stay intact after a
#      `;`. Pre-split this was protected anywhere in the sentence; post-split the
#      preceding-word check in `_is_proper_noun_fragment` calls
#      `_is_sentence_start_in` without threading `is_first_clause`, so "Vah" at
#      the clause start reads as a sentence opener (not a fragment) and "Kantos"
#      wrongly swaps to "you" ("…; Vah you bow"). Re-opens a shipped fix
#      (sq-playtest 2026-06-20/21).
#
# These tests are RED until Dev makes the clause-boundary explicit: thread
# `is_first_clause` into `_is_proper_noun_fragment`, and make
# `_is_sentence_start_in`'s `j < 0` walk-back return `clause_is_sentence_start`.
# The first-clause sanity guards below must STAY green — the fix must not
# over-correct and lowercase a genuine sentence-initial swap.
# ===========================================================================


def test_subject_swap_at_start_of_post_semicolon_clause_stays_lowercase():
    """A PC subject+verb swap at the start of a non-first `;`-clause is
    mid-sentence (a semicolon does not open a sentence), so it must render
    lowercase 'you', not 'You'. Pre-split this was correct; the `;`-split
    regressed it to capital 'You'."""
    text = "The torch gutters; Carl steadies it."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "The torch gutters; you steady it.", repr(out)
    assert "; You steady" not in out, repr(out)


def test_subject_and_possessive_swap_after_semicolon_stays_lowercase():
    """Both the subject name swap AND the follow-on possessive pronoun in a
    post-`;` armed clause must be lowercase: 'you raise your guard'. (The
    possessive 'his'->'your' agreement is correct from the green run; only the
    capitalization of the clause-initial 'you' regressed.)"""
    text = "The shield drops; Carl raises his guard."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "The shield drops; you raise your guard.", repr(out)
    assert "; You raise" not in out, repr(out)


def test_possessive_name_swap_at_start_of_post_semicolon_clause_stays_lowercase():
    """The Pass-1 possessive-name path has the same clause-boundary bug: a
    sentence-initial-looking 'Carl's' at the start of a non-first `;`-clause
    must become lowercase 'your', not 'Your'."""
    text = "The torch gutters; Carl's grip tightens."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "The torch gutters; your grip tightens.", repr(out)
    assert "; Your grip" not in out, repr(out)


def test_first_clause_subject_swap_stays_capitalized_after_semicolon_fix():
    """Sanity guard — the lowercase fix must NOT over-correct: a genuine
    sentence-initial swap (first clause / whole sentence) must STAY capital
    'You'. This pins that the fix targets only non-first `;`-clauses."""
    text = "Carl steadies the torch; the flame holds."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out.startswith("You steady the torch"), repr(out)


def test_suffix_npc_name_after_semicolon_left_intact_153_14_regression():
    """153-14 regression surfaced by the `;`-split: 'Vah Kantos' is a multi-word
    NPC name whose suffix token equals the PC name 'Kantos'. After a `;` it must
    stay fully intact — never 'Vah you bow'. The single-clause baseline
    (test_pc_name_suffix_of_npc_name_left_intact_mid_sentence) still passes, so
    the `;`-split is the regression."""
    text = "The door opens; Vah Kantos bows to the council."
    out, count = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert out == text, repr(out)
    assert count == 0


def test_prefix_npc_name_after_semicolon_left_intact_green_guard():
    """Companion green guard: the PREFIX collision ('Kantos Vah', PC 'Kantos')
    after a `;` is already protected (the following-capitalized-word check fires
    on 'Vah'). This must stay green through the fix — pins that the boundary fix
    does not regress the prefix case."""
    text = "The door opens; Kantos Vah studies the console."
    out, count = swap_to_second_person(text, target_name="Kantos", pronouns="he/him")
    assert out == text, repr(out)
    assert count == 0
