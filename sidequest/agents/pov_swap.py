"""2nd-person POV swap for narration prose (Story 49-8, antecedent-fix 2026-05-23).

Found in the 2026-05-12 caverns_sunden playtest: every per-PC narration
card landed on every player's tab third-person. On Carl's own tab his
action card should read "You plant a boot..." not "Carl plants a boot...".

This module rewrites NAME references to a single named target into
second-person, plus their immediate verb conjugation and sentence-local
reflexive ("himself"/"herself"/"themself"). Pure string transform — no
network, no LLM. Called by ``sidequest.server.emitters.emit_event`` once
per recipient when the recipient's PC name matches
``visibility_sidecar["anchor_pc"]``.

ANTECEDENT-BLINDNESS RETIRE (2026-05-23 pulp_noir/annees_folles repro):
the legacy helper carried antecedent-blind PRONOUN passes — every "he" /
"his" / "him" in the anchored prose was rewritten when the PC had he/him
pronouns. In any scene with an NPC who shares the PC's pronouns ("the
man with Le Figaro folds his paper… He doesn't hurry."), the pronoun
passes turned NPC actions into PC actions. Regex has no antecedent
resolution, so the passes were removed and the 2nd-person voice contract
for non-name pronouns shifted to the narrator side (see
``narrator_prompts/pov_rules.md`` — narrator writes the PC's actions
using the PC's NAME, never a pronoun).

ANTECEDENT-GATED PRONOUN RE-INTRODUCTION (Story 153-29, 2026-06-22
MP-PRONOUN-LOCALIZATION-INCOMPLETE): the narrator-side discipline alone
left possessive / subject / object pronouns for the SAME just-swapped PC
in third person — "Vesna presses her palm" localized to "You press her
palm" (want "your palm"), and worse, single combat sentences mixing 2nd +
3rd person for one character. The pronoun passes return GATED: a pronoun
is agreed only inside a ``;``-delimited CLAUSE that already had a
name-driven swap of the target PC. The clause-local gate is what makes
this safe — the 2026-05-23 NPC-bleed lived in a clause that never named
the PC, and that clause is now never armed.

Reflexives ("himself"/"herself"/"themself") survive but only fire when
the sentence already had a name-driven subject swap — without that gate
"himself" in a sentence about another character would mis-attach.

Dialogue inside double quotes is preserved unchanged — speakers
referring to the target by name belong to the in-world scene, not the
narrator voice.

Contract:

    swap_to_second_person(text, target_name="Carl", pronouns="he/him")
        -> ("You plant a boot...", 5)

Returns the rewritten text and a count of distinct substitutions for
the OTEL span ``narration.second_person_swap`` (GM-panel lie detector).
"""

from __future__ import annotations

import re

from opentelemetry import trace

_tracer = trace.get_tracer("sidequest.pov_swap")

# Pronoun forms keyed by canonical pronoun string.
# Only three canonical sets are supported — anything else raises so
# downstream prose never silently uses the wrong grammar.
_PRONOUN_FORMS = {
    "he/him": {
        "subject": "he",
        "object": "him",
        "possessive": "his",
        "reflexive": ("himself",),
        # When "his" appears, both possessive and predicate-adjective
        # forms map to "your" — there is no possessive-pronoun "his" vs
        # "his own" ambiguity to resolve here. Predicate "his" (as in
        # "the gun was his") also maps to "yours" but is not currently
        # tested; we err toward the more common possessive case.
        "possessive_alt_subject_form": None,  # "his" is not ambiguous
    },
    "she/her": {
        "subject": "she",
        "object": "her",
        "possessive": "her",  # same surface form as the object — disambiguated by lookahead
        "reflexive": ("herself",),
        "possessive_alt_subject_form": None,
    },
    "they/them": {
        "subject": "they",
        "object": "them",
        "possessive": "their",
        "reflexive": ("themself", "themselves"),
        "possessive_alt_subject_form": None,
    },
}


def project_to_canonical_pronouns(pronouns: str) -> str | None:
    """Project a (possibly freeform) pronoun string to a canonical grammatical
    set the localizer accepts — a key of :data:`_PRONOUN_FORMS`.

    Chargen permits freeform pronouns (builder.py ``pronouns_allow_freeform``):
    "she/they", "any", "xe/xem", "it/its", "ze/zir", and so on. The localizer
    :func:`swap_to_second_person` only knows the three canonical grammatical
    sets, so every caller must hand it a canonical value. This derives that
    value from the player's DISPLAY pronouns without discarding their choice
    (Story 158-14 — the player keeps their freeform pronouns; only the grammar
    handed to the localizer is canonicalized):

      - an already-canonical value maps to itself
      - a value naming she/her -> ``"she/her"``
      - else a value naming he/him/his -> ``"he/him"``
      - everything else (they, them, "any", neopronouns) -> ``"they/them"``

    Returns ``None`` only for an empty/blank input — there is no grammar to
    derive from nothing, and the caller treats that as a distinct skip reason.
    Matching is whole-token (split on non-letters) so "they" is never mistaken
    for the "he" substring it contains.
    """
    if not pronouns or not pronouns.strip():
        return None
    if pronouns in _PRONOUN_FORMS:
        return pronouns
    tokens = {t for t in re.split(r"[^a-z]+", pronouns.lower()) if t}
    if tokens & {"she", "her", "hers", "herself"}:
        return "she/her"
    if tokens & {"he", "him", "his", "himself"}:
        return "he/him"
    return "they/them"


# Irregular verbs that need explicit 3rd-person -> 2nd-person mapping.
# Regular -s/-es/-ies suffixes are handled by _conjugate's algorithmic
# fallback.
#
# Contraction stems (``doesn``, ``wasn``, ``isn``, ``hasn``) are included
# because the verb-capture regex is ``\w+`` — it stops at the apostrophe,
# so "Carl doesn't move" captures the bare stem "doesn" and leaves the
# "'t move" suffix outside the match. Conjugating the stem to its plural
# bare form composes correctly with that surviving suffix: "doesn" → "don"
# + "'t move" = "don't move". This matches the bug-2 sub-case found in the
# 2026-05-23 pulp_noir/annees_folles playtest ("You doesn't hurry").
_IRREGULAR_VERBS: dict[str, str] = {
    "has": "have",
    "is": "are",
    "was": "were",
    "does": "do",
    "goes": "go",
    "doesn": "don",
    "wasn": "weren",
    "isn": "aren",
    "hasn": "haven",
}


def _conjugate(verb: str) -> str:
    """Convert a 3rd-person-singular verb to its 2nd-person form.

    Examples:
        plants -> plant
        watches -> watch
        tries -> try
        has -> have
        is -> are
    """
    if not verb:
        return verb
    lower = verb.lower()
    if lower in _IRREGULAR_VERBS:
        replacement = _IRREGULAR_VERBS[lower]
        # Preserve capitalization of the first letter.
        if verb[0].isupper():
            return replacement[0].upper() + replacement[1:]
        return replacement
    if lower.endswith("ies") and len(lower) > 3:
        return verb[:-3] + "y"
    if lower.endswith(("sses", "shes", "ches", "xes", "zes")):
        return verb[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return verb[:-1]
    return verb


def _looks_like_verb(word: str) -> bool:
    """Heuristic: does ``word`` look like a 3rd-person-singular verb?

    Used to decide whether to conjugate the word following a subject
    swap. Conservative — non-verbs that happen to end in -s (plural
    nouns, possessives) will be left alone unless they're in a position
    that clearly demands a verb.
    """
    if not word:
        return False
    lower = word.lower()
    if lower in _IRREGULAR_VERBS:
        return True
    # Word ends in -s but not -ss is the basic 3rd-person-singular signal.
    # Don't treat plural nouns ending in -ies as verbs unless context demands.
    return lower.endswith("s") and not lower.endswith("ss")


def _is_pronoun(word: str) -> bool:
    """Check if a word is a pronoun (subject, object, or possessive form).

    Used in Pass 8 and Pass 9 to avoid treating pronouns as verbs/adverbs.
    A pronoun following a comma/"and" is never a coordinated verb of the
    swapped subject — it heads a clause about a different referent (an NPC
    or other actor), OR (for possessives like "its"/"his") modifies a noun.
    Either way it must never be conjugated/de-pluralized.

    Possessive forms that end in -s ("its", "hers", "ours", "yours",
    "theirs") are explicitly included: they otherwise satisfy the naive
    ``_looks_like_verb`` -s test and were being stripped to "it"/"her"/…
    ([BAR-2] 2026-06-06: "turning its eyeless gaze" → "turning it eyeless
    gaze"; #708 "his copper face" → "hi copper face").
    """
    if not word:
        return False
    lower = word.lower()
    # All canonical pronouns across all three pronoun sets
    pronouns = {
        # he/him set
        "he",
        "him",
        "his",
        # she/her set
        "she",
        "her",
        # they/them set
        "they",
        "them",
        "their",
        # Generic/other pronouns that should block Pass 8/9
        "i",
        "me",
        "we",
        "us",
        "you",
        "it",
        # Possessive / absolute forms that end in -s (would otherwise trip
        # the naive _looks_like_verb -s test).
        "its",
        "hers",
        "ours",
        "yours",
        "theirs",
        "mine",
    }
    return lower in pronouns


def _is_skippable_adverb(word: str) -> bool:
    """Whether ``word`` is a leading adverb the Pass 8/9 stranded-verb
    passes may skip over to reach the real coordinated verb.

    Deliberately narrow: only ``then`` and -ly adverbs qualify. The legacy
    test (``lowercase and not a pronoun``) admitted numbers ("four"),
    adjectives ("ivory"), and participles ("turning") as "adverbs", so the
    *next* word — a plural noun heading an absolute phrase — got conjugated
    ([BAR-2] "four arms"→"four arm", "ivory tusks"→"ivory tusk"). Regex
    cannot POS-tag, so the safe set is restricted to the two surface forms
    the Story 71-6 adverb-skip was actually built for ("…, then fires" /
    "…and slowly raises"). Anything else leaves the following word alone.
    """
    if not word:
        return False
    lower = word.lower()
    return lower == "then" or (lower.endswith("ly") and len(lower) > 2)


def _is_proper_noun_fragment(
    text: str, name_start: int, name_end: int, *, is_first_clause: bool = True
) -> bool:
    """Whether the PC-name token spanning ``[name_start, name_end)`` is part
    of a longer multi-word proper noun (an NPC's full name) rather than a
    standalone reference to the PC.

    ``text`` is a single ``;``-delimited clause (Story 153-29); ``is_first_clause``
    is threaded into the preceding-word sentence-start check so a capitalized
    preceding token at the start of a NON-first clause ("…; Vah Kantos bows")
    reads as a name fragment, not a sentence opener — otherwise the clause
    boundary masquerades as a sentence start and the 153-14 guard leaks
    ("…; Vah you bow", review round-trip 1).

    The ``\\b...\\b`` boundaries on the name passes are necessary but not
    sufficient: a multi-word NPC name like "Kantos Vah" carries an *internal*
    word boundary, so ``\\bKantos\\b`` still matches the "Kantos" token inside
    it. Without this guard the subject passes rewrote NPC "Kantos Vah" into
    "you Vah" on the player's tab (NPC-NAME-PCSUBSTRING-SUBSTITUTION,
    sq-playtest 2026-06-20/21).

    A multi-word proper noun is a run of capitalized words. Two adjacency
    signals mark the matched name as a fragment of one:

    * **Following word capitalized** — "Kantos Vah". A real subject-verb
      construction puts a lowercase verb after the name ("Kantos draws"), so a
      capitalized following token is a name continuation. Covers the
      documented prefix/infix case.
    * **Preceding word capitalized and not itself a sentence start** —
      "the envoy Vah Kantos" mid-clause. The sentence-start exclusion keeps an
      ordinary capitalized opener ("Then Kantos moves.") from being read as a
      name fragment — that opener is capitalized for position, not because it
      is a proper noun. (A trailing name fragment that *is* sentence-initial,
      e.g. "Vah Kantos …" opening a sentence, is genuinely ambiguous without a
      name list and is left to swap; the documented finding is prefix.)
    """
    after = re.match(r"\s+(\w)", text[name_end:])
    if after and after.group(1).isupper():
        return True
    before = re.search(r"(\w+)\s+$", text[:name_start])
    return bool(
        before
        and before.group(1)[0].isupper()
        and not _is_sentence_start_in(
            text, before.start(1), clause_is_sentence_start=is_first_clause
        )
    )


def _split_by_dialogue(text: str) -> list[tuple[str, str]]:
    """Split text into alternating prose / dialogue regions.

    Dialogue is anything inside straight double quotes (``"...".``).
    Returns a list of (kind, segment) pairs where ``kind`` is
    ``"prose"`` or ``"dialogue"``. Concatenating the segments
    reconstructs the input verbatim.
    """
    parts: list[tuple[str, str]] = []
    last = 0
    for m in re.finditer(r'"[^"]*"', text):
        if m.start() > last:
            parts.append(("prose", text[last : m.start()]))
        parts.append(("dialogue", m.group(0)))
        last = m.end()
    if last < len(text):
        parts.append(("prose", text[last:]))
    return parts


def extract_spoken_lines(text: str) -> list[str]:
    """Return the verbatim contents of each double-quoted span in ``text``.

    Used to surface player-spoken dialogue into the shared MP transcript
    (playtest 2026-05-17): the quoted spans are what the PC actually said
    aloud; stage direction outside the quotes is the player narrating
    their own action and is excluded. Inner whitespace is stripped and
    empty quotes are dropped — an empty utterance is not speech.

    Reuses :func:`_split_by_dialogue` so the dialogue regex stays
    single-sourced with the POV-swap path.
    """
    lines: list[str] = []
    for kind, segment in _split_by_dialogue(text):
        if kind != "dialogue":
            continue
        inner = segment[1:-1].strip()
        if inner:
            lines.append(inner)
    return lines


def _split_into_sentences(prose: str) -> list[str]:
    """Split a prose region into sentences, keeping each sentence's
    trailing punctuation attached.

    Splits on ``.`` ``?`` ``!`` followed by whitespace or end-of-string.
    Preserves the original whitespace/punctuation so concatenation
    reproduces the input verbatim.
    """
    # Pattern captures each sentence plus its trailing punctuation and
    # the whitespace that follows (so re-joining preserves spacing).
    pieces = re.split(r"(?<=[.!?])(\s+)", prose)
    # ``re.split`` returns alternating sentence / whitespace tokens.
    # Re-assemble back to "sentence with trailing space" tokens.
    sentences: list[str] = []
    i = 0
    while i < len(pieces):
        sent = pieces[i]
        trailing = pieces[i + 1] if (i + 1) < len(pieces) else ""
        if sent or trailing:
            sentences.append(sent + trailing)
        i += 2
    return sentences


def _rewrite_clause(
    clause: str,
    *,
    target_name: str,
    forms: dict,
    is_first_clause: bool,
) -> tuple[str, int]:
    """Apply all POV substitutions to a single ``;``-delimited clause.

    ``clause`` is one segment of an engine-sentence split on ``;`` (see
    :func:`_rewrite_sentence`). Processing per-clause makes the Story 153-29
    pronoun-agreement gate CLAUSE-local: ``name_swap_occurred`` is a flag
    scoped to THIS clause, so a name swap here never licenses pronoun
    rewrites in a sibling clause about a different subject.

    Returns ``(rewritten_clause, count)`` where ``count`` is the total
    number of substitutions performed (used for the OTEL swap_count
    attribute).
    """
    count = 0
    text = clause
    had_subject_swap = False
    # Story 153-29 pronoun gate: armed by ANY name-driven swap of the target
    # PC in this clause — Pass 1 (possessive name), Pass 2 (subject+verb), or
    # Pass 3 (bare name). Gates the re-introduced possessive/subject/object
    # pronoun passes so they only agree pronouns that co-refer with the PC.
    name_swap_occurred = False
    # Pass 2b gating: whether the sentence subject was swapped to "You"
    # (Pass 2 fired, or Pass 3 swapped a sentence-initial bare name), and
    # whether Pass 2 actually found the real verb immediately adjacent to
    # the name. When the subject swapped but no adjacent verb was found,
    # the real verb is stranded behind an interrupter (adverb / appositive
    # / parenthetical) and Pass 2b must conjugate it.
    subj_swapped_at_start = False
    pass2_found_adjacent_verb = False

    name_esc = re.escape(target_name)

    # ------------------------------------------------------------------
    # Pass 1: possessive name "Carl's" -> "Your"/"your" (attributive)
    #         or "Yours"/"yours" (predicate/absolute).
    #
    # English distinguishes:
    #   attributive  "Carl's polearm"  → "Your polearm"   (governs a noun)
    #   predicate    "The polearm was Carl's." → "...was yours."  (stands alone)
    #
    # Predicate position: {Name}'s is followed by terminal punctuation
    # (.!?…), a comma, semicolon, colon, end-of-text, or a coordinating
    # conjunction (and/or/but/nor/so/yet).  Anything else is attributive.
    # ------------------------------------------------------------------
    def _pos_name_sub(m: re.Match) -> str:
        nonlocal count, name_swap_occurred
        count += 1
        name_swap_occurred = True
        at_start = _is_sentence_start_in(text, m.start(), clause_is_sentence_start=is_first_clause)
        rest = text[m.end() :]
        stripped = rest.lstrip()
        is_predicate = (
            not stripped  # end of string
            or stripped[0] in ".!?,;:…"  # terminal punctuation or clause boundary
            or bool(re.match(r"\b(?:and|or|but|nor|so|yet)\b", stripped))  # coordinating conj
        )
        if is_predicate:
            return "Yours" if at_start else "yours"
        return "Your" if at_start else "your"

    text = re.sub(rf"\b{name_esc}'s\b", _pos_name_sub, text)

    # ------------------------------------------------------------------
    # Pass 2: subject name + immediate verb. "Carl plants" -> "You plant".
    # We swap the name and conjugate the verb in one pass so the
    # verb-following-the-subject is reliably handled.
    # ------------------------------------------------------------------
    def _name_subj_sub(m: re.Match) -> str:
        nonlocal count, had_subject_swap, subj_swapped_at_start
        nonlocal pass2_found_adjacent_verb, name_swap_occurred
        # The PC name as a fragment of a longer NPC proper noun ("Kantos Vah")
        # must not swap — leave the full NPC name intact (Story 153-14).
        if _is_proper_noun_fragment(
            text, m.start(), m.start() + len(target_name), is_first_clause=is_first_clause
        ):
            return m.group(0)
        had_subject_swap = True
        name_swap_occurred = True
        verb = m.group(1)
        at_start = _is_sentence_start_in(text, m.start(), clause_is_sentence_start=is_first_clause)
        if at_start:
            subj_swapped_at_start = True
        # If the token immediately after the name is itself a 3rd-person
        # verb, Pass 2 conjugates the real verb here and there is no
        # stranding — Pass 2b must NOT fire. If it's a non-verb (an
        # adverb interrupter), the real verb is downstream and stranded.
        if _looks_like_verb(verb):
            pass2_found_adjacent_verb = True
        you = "You" if at_start else "you"
        conjugated = _conjugate(verb)
        # Count the subject swap; count the verb conjugation separately
        # when it actually changes the verb form so swap_count reflects
        # the true number of edits.
        count += 1
        if conjugated != verb:
            count += 1
        return f"{you} {conjugated}"

    text = re.sub(rf"\b{name_esc}\b\s+(\w+)", _name_subj_sub, text)

    # ------------------------------------------------------------------
    # Pass 3: bare name (no following verb) -> "you"/"You". Catches
    # vocative or trailing-clause uses like "...nodded at Carl."
    # ------------------------------------------------------------------
    def _name_bare_sub(m: re.Match) -> str:
        nonlocal count, subj_swapped_at_start, name_swap_occurred
        # Same fragment guard as Pass 2: a bare PC-name token inside a longer
        # NPC proper noun ("Kantos Vah") must not swap to "you" (Story 153-14).
        if _is_proper_noun_fragment(text, m.start(), m.end(), is_first_clause=is_first_clause):
            return m.group(0)
        count += 1
        name_swap_occurred = True
        at_start = _is_sentence_start_in(text, m.start(), clause_is_sentence_start=is_first_clause)
        # A sentence-initial bare name is the grammatical subject. Pass 2
        # was blocked here (a comma/dash appositive defeats its ``\s+``),
        # so record the subject swap for Pass 2b. Mid-sentence bare names
        # (object / vocative) are NOT subjects — don't flag them.
        if at_start:
            subj_swapped_at_start = True
        return "You" if at_start else "you"

    text = re.sub(rf"\b{name_esc}\b", _name_bare_sub, text)

    # ------------------------------------------------------------------
    # Pass 2b: subject-verb interrupter. Pass 2 only conjugates the token
    # immediately after the name; an adverb / appositive / parenthetical
    # between the subject and its verb leaves the real main verb stranded
    # in 3rd-person form ("You steadily works…" / "You, crouched at the
    # rim, works…" / "You — still braced — works…"). When the sentence
    # subject was swapped to "You" but Pass 2 did NOT find the real verb
    # adjacent, scan past the bounded interrupter and conjugate the first
    # stranded verb. (sq-playtest 2026-05-17 / [BS-BUG-LOW])
    # ------------------------------------------------------------------
    if subj_swapped_at_start and not pass2_found_adjacent_verb:
        interrupter_pat = re.compile(
            r"^(\s*You\b)"
            r"("
            r",[^,]*?,"  # , appositive ,
            r"|\s*[—–-][^—–-]*?[—–-]"  # — parenthetical — (em/en dash or hyphen)
            r"|\s+\([^)]*?\)"  # ( parenthetical )
            r"|(?:\s+\w+)+?"  # leading adverb(s)
            r")\s+(\w+)"
        )

        def _interrupter_verb_sub(m: re.Match) -> str:
            nonlocal count
            verb = m.group(3)
            if not _looks_like_verb(verb):
                return m.group(0)
            conjugated = _conjugate(verb)
            if conjugated == verb:
                return m.group(0)
            count += 1
            return f"{m.group(1)}{m.group(2)} {conjugated}"

        text = interrupter_pat.sub(_interrupter_verb_sub, text, count=1)

    # ------------------------------------------------------------------
    # Pass 4: reflexive ("himself"/"herself"/"themself"/"themselves") -> "yourself"
    #
    # Gated on ``had_subject_swap``: fires only when this sentence already
    # had a name-driven subject swap (Pass 2). Without the gate, a sentence
    # like "The man crosses himself" rewrote "himself" → "yourself" simply
    # because the PC shared he/him pronouns — even though the reflexive
    # unambiguously refers to "the man". Sentence-local antecedent means
    # the reflexive belongs to the PC only when the PC IS the local
    # subject — which Pass 2 establishes when (and only when) the PC's
    # name appears as that subject.
    # ------------------------------------------------------------------
    if had_subject_swap:
        for reflexive in forms["reflexive"]:
            count_before = count

            def _reflexive_sub(m: re.Match) -> str:
                nonlocal count
                count += 1
                return "yourself"

            text, n = re.subn(rf"\b{re.escape(reflexive)}\b", _reflexive_sub, text)
            # subn returns the count separately — but our nested function
            # already incremented; reset by removing the auto-count and
            # using subn's count. Simpler: subtract our increment, add subn.
            # (re.subn here is the authoritative count.)
            count = count_before + n

    # ------------------------------------------------------------------
    # Passes 5/6/7 (subject / possessive / object pronoun agreement) were
    # RETIRED 2026-05-23 (antecedent-blind NPC bleed) and RE-INTRODUCED
    # antecedent-gated by Story 153-29 — see the gated block AFTER Pass 9,
    # which runs once the name-driven passes have armed ``name_swap_occurred``
    # for this clause. (They run last so the subject/object/possessive forms
    # are matched against text the name passes have already settled.)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Pass 8: "and <verb>" continuation. When this sentence had a
    # subject swap earlier, the implicit subject after "and" is still
    # "you" — conjugate the verb if it's in 3rd-person form.
    #
    # Adverb-skip extension (Story 71-6): if a single leading adverb /
    # "then" sits between "and" and the real verb, capture both words so
    # the verb can be conjugated.  Examples:
    #   "…and slowly raises…"  → "…and slowly raise…"
    #   "…and then fires…"     → "…and then fire…"
    # The second-word group is optional — falls back to original
    # single-word behaviour when no adverb is present.
    # ------------------------------------------------------------------
    if had_subject_swap:

        def _and_verb_sub(m: re.Match) -> str:
            nonlocal count
            word1 = m.group(1)
            word2 = m.group(2)
            if word2 is None:
                # Single word after "and" — original behaviour. Pronouns are
                # never verbs ("and his …"/"and its …" must not be stripped).
                if not _looks_like_verb(word1) or _is_pronoun(word1):
                    return m.group(0)
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f"and {conjugated}"
            # Two words captured: "and <word1> <word2>".
            if _looks_like_verb(word1) and not _is_pronoun(word1):
                # word1 is the verb (no adverb before it).
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f"and {conjugated} {word2}"
            if _is_skippable_adverb(word1) and _looks_like_verb(word2) and not _is_pronoun(word2):
                # word1 is a leading adverb/"then" — skip it, conjugate word2.
                # Restricting to real adverbs (not "any lowercase non-pronoun")
                # stops numbers/adjectives/participles ("four arms", "ivory
                # tusks", "turning its") from being mistaken for adverbs and
                # their following plural noun de-pluralized ([BAR-2]).
                conjugated = _conjugate(word2)
                if conjugated == word2:
                    return m.group(0)
                count += 1
                return f"and {word1} {conjugated}"
            return m.group(0)

        text = re.sub(r"\band\s+(\w+)(?:\s+(\w+))?", _and_verb_sub, text)

    # ------------------------------------------------------------------
    # Pass 9: ", <verb>" comma-coordinated continuation. Same logic as
    # Pass 8 but for verb-coordination through commas instead of "and".
    # English narration commonly chains actions across commas without
    # repeating the subject: "Willes thumbs the flap, sets the fitting,
    # and works the curl onto parchment." Pass 2 catches "thumbs"; Pass
    # 8 catches "works"; without this pass "sets" stays in 3rd-person
    # form and the rewritten prose reads "you thumb..., sets..., and
    # work..." — mixed conjugation in a single sentence
    # (sq-playtest 2026-05-15).
    #
    # Gated by ``had_subject_swap`` so we don't conjugate commas that
    # AREN'T verb-coordination (appositives, parentheticals, relative-
    # clause boundaries). Further gated by ``_looks_like_verb`` so plural
    # nouns or commas-before-articles ("..., the bronze fitting") pass
    # through unchanged.
    #
    # Adverb-skip extension (Story 71-6): mirrors Pass 8 — if a single
    # leading adverb/"then" sits between the comma and the real verb,
    # capture both words so the verb can be conjugated.  Example:
    #   "…steadies it, then fires."  → "…steady it, then fire."
    # ------------------------------------------------------------------
    if had_subject_swap:

        def _comma_verb_sub(m: re.Match) -> str:
            nonlocal count
            word1 = m.group(1)
            word2 = m.group(2)
            # Pass 8 owns the "and <verb>" surface.
            if word1.lower() == "and":
                return m.group(0)
            if word2 is None:
                # Single word after comma — original behaviour. Pronouns are
                # never verbs (", his …"/", its …" must not be stripped to
                # "hi"/"it" — [BAR-2] / #708).
                if not _looks_like_verb(word1) or _is_pronoun(word1):
                    return m.group(0)
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f", {conjugated}"
            # Two words captured: ", <word1> <word2>".
            if word2.lower() == "and":
                # ", word and …" — let Pass 8 handle the "and <verb>" part.
                return m.group(0)
            if _looks_like_verb(word1) and not _is_pronoun(word1):
                # word1 is the verb (no adverb before it).
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f", {conjugated} {word2}"
            if _is_skippable_adverb(word1) and _looks_like_verb(word2) and not _is_pronoun(word2):
                # word1 is a leading adverb/"then" — skip it, conjugate word2.
                # Restricting to real adverbs (not "any lowercase non-pronoun")
                # stops a number/adjective/participle ("four arms", "ivory
                # tusks", "turning its") from being read as an adverb and its
                # following plural noun / possessive de-pluralized ([BAR-2]).
                conjugated = _conjugate(word2)
                if conjugated == word2:
                    return m.group(0)
                count += 1
                return f", {word1} {conjugated}"
            return m.group(0)

        text = re.sub(r",\s+(\w+)(?:\s+(\w+))?", _comma_verb_sub, text)

    # ------------------------------------------------------------------
    # Pass 9b: bare "then <verb>" continuation (Story 158-38). Passes 8 and 9
    # re-conjugate a verb coordinated by "and" or "," (including an
    # "and then" / ", then" adverb-skip), but a verb coordinated by a BARE
    # "then" with no preceding "and"/"," was left stranded in 3rd person:
    # "...the anchor then grips the rope" stayed "then grips" (want "then
    # grip"; pingpong 2026-06-23 MP beneath_sunden). Same ``had_subject_swap``
    # gate and ``_looks_like_verb``/``_is_pronoun`` heuristics as Pass 8/9 so
    # it stays clause-local and never de-pluralizes a following noun
    # ("then the gate slams" leaves "slams" — its subject is "the gate", not
    # "you"; "then" before a non-verb is a no-op).
    # ------------------------------------------------------------------
    if had_subject_swap:

        def _then_verb_sub(m: re.Match) -> str:
            nonlocal count
            word = m.group(1)
            if not _looks_like_verb(word) or _is_pronoun(word):
                return m.group(0)
            conjugated = _conjugate(word)
            if conjugated == word:
                return m.group(0)
            count += 1
            return f"then {conjugated}"

        text = re.sub(r"\bthen\s+(\w+)", _then_verb_sub, text)

    # ------------------------------------------------------------------
    # Passes 5/6/7 RE-INTRODUCED, ANTECEDENT-GATED (Story 153-29,
    # MP-PRONOUN-LOCALIZATION-INCOMPLETE, sq-playtest 2026-06-20/21).
    #
    # The name + adjacent-verb swap (Passes 1-3) leaves possessive / subject /
    # object pronouns for the SAME just-swapped PC in third person, so the
    # localized player reads person-disagreement on their OWN tab:
    #   "Vesna presses her palm"  ->  "You press her palm"   (want "your palm")
    # and, worse, a single combat sentence mixing 2nd + 3rd person for one
    # character ("…lands on your back and something rakes across his shoulders").
    #
    # These three pronoun passes were RETIRED 2026-05-23 because they were
    # antecedent-blind — they rewrote NPC pronouns too ("You doesn't hurry").
    # They return GATED on ``name_swap_occurred``: a pronoun is only agreed
    # when THIS clause already had a name-driven swap of the target PC (armed
    # by Pass 1 possessive-name OR Pass 2/3 subject-name). Because
    # ``_rewrite_clause`` runs per ``;``-delimited clause, the gate is
    # CLAUSE-local — a same-pronoun NPC in a later ``;``-clause that never
    # named the PC stays fully third-person (preserves the 2026-05-23 fix —
    # AC 4: "Carl plants a boot; the moth shudders against him" keeps "him").
    #
    # Forms come from ``_PRONOUN_FORMS`` so we only ever convert the PC's OWN
    # pronoun set; an unrelated set in the clause is left alone. For she/her
    # the possessive and object surface forms are both "her" — split by a
    # following-noun lookahead (possessive governs a noun; object does not).
    # ------------------------------------------------------------------
    if name_swap_occurred:
        subj_form = forms["subject"]
        obj_form = forms["object"]
        poss_form = forms["possessive"]
        her_is_ambiguous = poss_form == obj_form  # she/her: "her" is both

        # --- Pass 6: possessive pronoun -> "your" / "Your" ---
        poss_pat = (
            rf"\b{re.escape(poss_form)}\b(?=\s+\w)"
            if her_is_ambiguous
            else rf"\b{re.escape(poss_form)}\b"
        )

        def _poss_pron_sub(m: re.Match) -> str:
            nonlocal count
            count += 1
            at_start = _is_sentence_start_in(
                text, m.start(), clause_is_sentence_start=is_first_clause
            )
            return "Your" if at_start else "your"

        text = re.sub(poss_pat, _poss_pron_sub, text)

        # --- Pass 5: subject pronoun -> "you" / "You" (+ conjugate the verb) ---
        # The follow-on subject pronoun co-refers with the swapped "You", so its
        # verb takes the 2nd-person form exactly like Pass 2 ("she strikes" ->
        # "you strike"). A non-verb / modal ("he can raise") just converts the
        # pronoun. ``_is_pronoun`` guards against de-pluralizing a trailing
        # pronoun mistaken for a verb.
        def _subj_pron_sub(m: re.Match) -> str:
            nonlocal count
            at_start = _is_sentence_start_in(
                text, m.start(), clause_is_sentence_start=is_first_clause
            )
            you = "You" if at_start else "you"
            count += 1
            following = m.group(1)
            if following is None:
                return you
            if _looks_like_verb(following) and not _is_pronoun(following):
                conjugated = _conjugate(following)
                if conjugated != following:
                    count += 1
                    return f"{you} {conjugated}"
            return f"{you} {following}"

        text = re.sub(rf"\b{re.escape(subj_form)}\b(?:\s+(\w+))?", _subj_pron_sub, text)

        # --- Pass 7: object pronoun -> "you" / "You" ---
        # For she/her the object "her" is the one NOT governing a noun (the
        # possessive pass already consumed the noun-governing occurrences).
        obj_pat = (
            rf"\b{re.escape(obj_form)}\b(?!\s+\w)"
            if her_is_ambiguous
            else rf"\b{re.escape(obj_form)}\b"
        )

        def _obj_pron_sub(m: re.Match) -> str:
            nonlocal count
            count += 1
            at_start = _is_sentence_start_in(
                text, m.start(), clause_is_sentence_start=is_first_clause
            )
            return "You" if at_start else "you"

        text = re.sub(obj_pat, _obj_pron_sub, text)

    # ------------------------------------------------------------------
    # Pass 10: subject-auxiliary inversion (Story 158-38). In a question the
    # auxiliary precedes its subject ("does Carl mean to go" -> name-swap ->
    # "does you mean to go"); the name + following-verb passes only ever
    # conjugate the verb AFTER the subject, so the leading 3rd-person auxiliary
    # was left disagreeing ("does you" / "has you" / "is you" / "was you";
    # pingpong 2026-06-23). Re-agree an irregular auxiliary sitting immediately
    # before the swapped "you".
    #
    # Gated on a "?" in the clause so a DECLARATIVE object-"you" is never
    # touched — "It is you." and "the dragon has you in its claws" are not
    # interrogative inversions ("you" is the predicate/object, not the inverted
    # subject), and only the small irregular-auxiliary set is eligible, so a
    # lexical "tells you" / "watches you" passes through unchanged.
    # ------------------------------------------------------------------
    if name_swap_occurred and "?" in text:

        def _inverted_aux_sub(m: re.Match) -> str:
            nonlocal count
            aux = m.group(1)
            if aux.lower() not in _IRREGULAR_VERBS:
                return m.group(0)
            conjugated = _conjugate(aux)
            if conjugated == aux:
                return m.group(0)
            count += 1
            return f"{conjugated} {m.group(2)}"

        text = re.sub(r"\b(\w+)\s+(you|You)\b", _inverted_aux_sub, text)

    return text, count


def _rewrite_sentence(
    sentence: str,
    *,
    target_name: str,
    forms: dict,
) -> tuple[str, int]:
    """Apply all POV substitutions to one engine-sentence, clause by clause.

    The sentence is split on ``;`` so the antecedent-gated pronoun passes
    (Story 153-29) stay CLAUSE-local: a name swap in one clause never licenses
    pronoun agreement in a later ``;``-clause about a different subject (the
    2026-05-23 NPC-bleed bug lived inside one engine "sentence" — the splitter
    only breaks on ``.!?``). ``;`` separators are preserved verbatim so the
    rejoin reproduces the input exactly.

    Returns ``(rewritten_sentence, count)``.
    """
    total = 0
    out: list[str] = []
    is_first = True
    for part in re.split(r"(;)", sentence):
        if part == ";":
            out.append(part)
            continue
        new_part, n = _rewrite_clause(
            part,
            target_name=target_name,
            forms=forms,
            is_first_clause=is_first,
        )
        out.append(new_part)
        total += n
        is_first = False
    return "".join(out), total


def _is_sentence_start_in(text: str, idx: int, *, clause_is_sentence_start: bool = True) -> bool:
    """Return True if position ``idx`` in ``text`` is the start of a
    sentence.

    A position counts as a sentence start when any of these hold:

    * It is the beginning of the string AND ``clause_is_sentence_start``.
      ``text`` here is a single ``;``-delimited clause (Story 153-29); only
      the FIRST clause of an engine-sentence opens a sentence, so a name
      swapped at the start of a later ``;``-clause must NOT capitalize
      (``…; Carl steps`` → ``…; you step``, lowercase). The caller passes
      ``clause_is_sentence_start=False`` for non-first clauses.
    * The preceding non-space char is terminal punctuation (``.!?``) or
      a Unicode ellipsis (``…``).
    * A paragraph break (``\\n\\n`` or more) sits between ``idx`` and
      the prior content. Narrator prose frequently closes a beat with an
      em-dash, en-dash, or colon and then opens the next paragraph with
      a name — the paragraph break alone is enough to make the next
      capital-letter a sentence start, regardless of the prior char.
      (2026-05-18 MP playtest: "Laverne's hand" rendered as lowercase
      "your hand" because an em-dash before ``\\n\\n`` defeated the
      ``.!?`` check.)
    """
    if idx == 0:
        return clause_is_sentence_start
    # Paragraph break — if the prior content contains a blank line and
    # everything between that line and idx is whitespace, we are at the
    # start of a new paragraph (and therefore a new sentence).
    prefix = text[:idx]
    if "\n\n" in prefix:
        tail_after_break = prefix.rsplit("\n\n", 1)[-1]
        if tail_after_break.strip() == "":
            return True
    j = idx - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0:
        # Walked back over leading whitespace to the start of this clause. After
        # the Story 153-29 `;`-split each clause carries a leading space (the
        # space that followed the `;`), so a clause-initial token sits at idx 1+
        # and reaches HERE, not the idx==0 branch. A clause start is only a
        # sentence start for the FIRST clause — otherwise "…; Carl steps" would
        # capitalize mid-sentence ("…; You step") and the 153-14 fragment guard
        # would misread a post-`;` name fragment as a sentence opener (review
        # round-trip 1). Honor the caller's clause context.
        return clause_is_sentence_start
    return text[j] in ".!?…"


def swap_to_second_person(
    text: str,
    *,
    target_name: str,
    pronouns: str,
    origin: str = "live",
) -> tuple[str, int]:
    """Rewrite third-person references to ``target_name`` into second-person.

    Args:
        text: Narration prose (may contain dialogue in double quotes,
            which is left unchanged).
        target_name: The PC name to swap to "You". Must be non-empty.
        pronouns: One of ``"he/him"``, ``"she/her"``, ``"they/them"``.
            Drives pronoun substitution and reflexive choice.
        origin: Provenance marker stamped on the ``narration.second_person_swap``
            OTEL span — ``"live"`` for the emit fan-out, ``"replay"`` for the
            resume/reconnect reconstruction (Story 158-38). Lets the GM panel
            tell a replayed swap from a live one and prove the replay path
            engaged rather than silently shipping stored 3rd-person prose.

    Returns:
        ``(rewritten_text, swap_count)`` — the count is the total number
        of substitutions performed across all passes (subject swaps,
        pronouns, reflexives, possessives, verb conjugations after
        ``and``). Used as the OTEL ``swap_count`` attribute.

    Raises:
        ValueError: If ``target_name`` is empty or ``pronouns`` is not
            one of the supported canonical strings. Silent fallback
            would inject wrong grammar into player-facing prose; fail
            loud per project policy.
    """
    if not target_name:
        raise ValueError("target_name must be non-empty")
    if pronouns not in _PRONOUN_FORMS:
        raise ValueError(
            f"unsupported pronouns: {pronouns!r}; supported: {sorted(_PRONOUN_FORMS.keys())}"
        )

    forms = _PRONOUN_FORMS[pronouns]
    total_count = 0

    out_parts: list[str] = []
    for kind, segment in _split_by_dialogue(text):
        if kind == "dialogue":
            out_parts.append(segment)
            continue
        # Process each sentence in this prose region.
        sentence_parts: list[str] = []
        for sentence in _split_into_sentences(segment):
            new_sent, count = _rewrite_sentence(
                sentence,
                target_name=target_name,
                forms=forms,
            )
            sentence_parts.append(new_sent)
            total_count += count
        out_parts.append("".join(sentence_parts))

    result = "".join(out_parts)

    with _tracer.start_as_current_span("narration.second_person_swap") as span:
        span.set_attribute("swap_target_name", target_name)
        span.set_attribute("swap_count", total_count)
        span.set_attribute("origin", origin)

    return result, total_count
