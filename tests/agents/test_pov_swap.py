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
    must become "You steadily work the drum with his hands, checking the
    rope." — not "You steadily works…". Under the retired-pronoun-passes
    contract, the possessive "his" survives (NPC-disambiguation cost);
    the narrator should be writing "Carl's hands" in well-formed prose.
    """
    text = "Carl steadily works the drum with his hands, checking the rope."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    assert out == "You steadily work the drum with his hands, checking the rope.", out
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
    they/them PC (the playgroup's Katia profile). The possessive "their"
    survives under the retired-pronoun-passes contract."""
    text = "Sam carefully works the drum with their hands."
    out, _ = swap_to_second_person(text, target_name="Sam", pronouns="they/them")
    assert out == "You carefully work the drum with their hands.", out
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


def test_pronoun_in_predicate_after_name_swap_stays_third_person():
    """If the narrator slips and mixes the PC's name with pronouns inside one
    sentence (legacy 49-8 narrator style), the name swaps but the pronouns
    survive — the renderer no longer guesses which pronouns refer to the PC
    vs. another character. The prompt-side discipline (pov_rules.md) is what
    keeps this from happening in well-formed prose."""
    text = "Carl plants a boot and he hauls the polearm out wet."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    # Name → "You", verb conjugated by Pass 2. The bare "he hauls" survives
    # untouched — antecedent-blind pronoun-pass would have wrongly converted
    # it; the new contract does not.
    assert out == "You plant a boot and he hauls the polearm out wet.", out


def test_object_pronoun_him_for_npc_stays_third_person():
    """Object 'him' referring to an NPC in the same sentence as the PC is a
    direct antecedent collision. The retire keeps the NPC pronoun intact."""
    text = "Carl plants a boot; the moth shudders against him."
    out, _ = swap_to_second_person(text, target_name="Carl", pronouns="he/him")
    # 'him' refers to the moth (or to Carl — ambiguous), and the engine no
    # longer guesses. 'Carl' → 'You' fires; the object pronoun stays.
    assert out == "You plant a boot; the moth shudders against him.", out


def test_object_pronoun_her_for_npc_stays_third_person():
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
