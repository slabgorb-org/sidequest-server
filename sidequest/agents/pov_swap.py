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
resolution; the only safe rewrites are NAME-driven. The 2nd-person voice
contract for non-name pronouns has been shifted to the narrator side
(see ``narrator_prompts/pov_rules.md`` — narrator writes the PC's actions
using the PC's NAME, never a pronoun, so this rewriter has unambiguous
input to swap).

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
    
    Used in Pass 8 and Pass 9 to avoid treating pronouns as adverbs.
    When word1 is a pronoun, we're not in an adverb-stranded-verb situation;
    we're in a clause about a different character (an NPC or other actor).
    """
    if not word:
        return False
    lower = word.lower()
    # All canonical pronouns across all three pronoun sets
    pronouns = {
        # he/him set
        "he", "him", "his",
        # she/her set  
        "she", "her",
        # they/them set
        "they", "them", "their",
        # Generic/other pronouns that should block Pass 8/9
        "i", "me", "we", "us", "you", "it",
    }
    return lower in pronouns


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


def _rewrite_sentence(
    sentence: str,
    *,
    target_name: str,
    forms: dict,
) -> tuple[str, int]:
    """Apply all POV substitutions to a single sentence.

    Returns ``(rewritten_sentence, count)`` where ``count`` is the total
    number of substitutions performed (used for the OTEL swap_count
    attribute).
    """
    count = 0
    text = sentence
    had_subject_swap = False
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
        nonlocal count
        count += 1
        at_start = (m.start() == 0) or _is_sentence_start_in(text, m.start())
        rest = text[m.end():]
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
        nonlocal pass2_found_adjacent_verb
        had_subject_swap = True
        verb = m.group(1)
        at_start = (m.start() == 0) or _is_sentence_start_in(text, m.start())
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
        nonlocal count, subj_swapped_at_start
        count += 1
        at_start = (m.start() == 0) or _is_sentence_start_in(text, m.start())
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
    # Passes 5/6/7 RETIRED (2026-05-23, sq-playtest pulp_noir/annees_folles).
    #
    # Pass 5 (subject pronoun "He"/"She"/"They" → "You"),
    # Pass 6 (possessive pronoun "his"/"her"/"their" → "your"), and
    # Pass 7 (object pronoun "him"/"her"/"them" → "you") are removed.
    #
    # All three were antecedent-blind: they fired on every matching pronoun
    # in the anchored prose regardless of who that pronoun actually referred
    # to. In a scene with an NPC who shared the PC's pronouns (the man with
    # Le Figaro folds *his* paper… *He* doesn't hurry) the passes converted
    # the NPC's actions into PC actions on the player's tab ("You doesn't
    # hurry"). Regex has no antecedent resolution; the fix is to constrain
    # the narrator-side input (see ``narrator_prompts/pov_rules.md``: write
    # the PC's actions using the PC's NAME, never a pronoun) and let the
    # surviving name-driven passes do the rest.
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
                # Single word after "and" — original behaviour.
                if not _looks_like_verb(word1):
                    return m.group(0)
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f"and {conjugated}"
            # Two words captured: "and <word1> <word2>".
            if _looks_like_verb(word1):
                # word1 is the verb (no adverb before it).
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f"and {conjugated} {word2}"
            if _looks_like_verb(word2) and not _is_pronoun(word1):
                # word1 is a leading adverb/then (not a pronoun) — skip it, conjugate word2.
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
                # Single word after comma — original behaviour.
                if not _looks_like_verb(word1):
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
            if _looks_like_verb(word1):
                # word1 is the verb (no adverb before it).
                conjugated = _conjugate(word1)
                if conjugated == word1:
                    return m.group(0)
                count += 1
                return f", {conjugated} {word2}"
            if _looks_like_verb(word2) and not _is_pronoun(word1):
                # word1 is a leading adverb/then (not a pronoun) — skip it, conjugate word2.
                conjugated = _conjugate(word2)
                if conjugated == word2:
                    return m.group(0)
                count += 1
                return f", {word1} {conjugated}"
            return m.group(0)

        text = re.sub(r",\s+(\w+)(?:\s+(\w+))?", _comma_verb_sub, text)

    return text, count


def _is_sentence_start_in(text: str, idx: int) -> bool:
    """Return True if position ``idx`` in ``text`` is the start of a
    sentence.

    A position counts as a sentence start when any of these hold:

    * It is the beginning of the string.
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
        return True
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
        return True
    return text[j] in ".!?…"


def swap_to_second_person(
    text: str,
    *,
    target_name: str,
    pronouns: str,
) -> tuple[str, int]:
    """Rewrite third-person references to ``target_name`` into second-person.

    Args:
        text: Narration prose (may contain dialogue in double quotes,
            which is left unchanged).
        target_name: The PC name to swap to "You". Must be non-empty.
        pronouns: One of ``"he/him"``, ``"she/her"``, ``"they/them"``.
            Drives pronoun substitution and reflexive choice.

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

    return result, total_count
