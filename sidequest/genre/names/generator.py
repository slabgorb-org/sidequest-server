"""Template-based name generator with corpus blending.

Combines Markov chain word generation with cultural naming patterns.
Each culture defines slots (given_name, family_name, etc.) that can draw from
Markov-trained corpora or static word lists. Templates like "{given_name} de {family_name}"
assemble slots into full names.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from sidequest.genre.models.culture import Culture
from sidequest.genre.names.markov import MarkovChain, generate_dictionary
from sidequest.genre.names.thresholds import (
    FAIL_BELOW_WORDS,
    STEM_OVERLAP_MIN,
    WARN_BELOW_WORDS,
    count_words,
)
from sidequest.telemetry.spans import (
    SPAN_NAMEGEN_FAIL_LOUD,
    SPAN_NAMEGEN_THIN_CORPUS,
    Span,
)

logger = logging.getLogger(__name__)


@dataclass
class SlotGenerator:
    """Generates words for a single naming slot."""

    chain: MarkovChain | None = None
    word_list: list[str] = field(default_factory=list)
    min_length: int = 4
    max_length: int = 12
    rng: random.Random | None = None

    def _random(self) -> random.Random:
        return self.rng if self.rng is not None else random._inst

    def generate(self) -> str:
        rng = self._random()

        has_chain = self.chain is not None
        has_list = bool(self.word_list)

        if not has_chain and not has_list:
            return ""

        if has_chain and has_list:
            use_chain = rng.random() < 0.67
        elif has_chain:
            use_chain = True
        else:
            use_chain = False

        if use_chain and self.chain is not None:
            for _ in range(20):
                word = self.chain.make_word()
                if (
                    self.min_length <= len(word) <= self.max_length
                    and word not in self.chain.reject_words
                ):
                    return word
            return self.chain.make_word()
        else:
            return rng.choice(self.word_list)


class _SlotMap:
    """Lazy dict-like object for str.format_map() with per-slot caching."""

    def __init__(self, slots: dict[str, SlotGenerator]) -> None:
        self._slots = slots
        self._cache: dict[str, str] = {}

    def __getitem__(self, key: str) -> str:
        if key not in self._cache:
            gen = self._slots.get(key)
            if gen is None:
                return "{" + key + "}"
            self._cache[key] = gen.generate()
        return self._cache[key]


@dataclass
class NameGenerator:
    """Generates names from template patterns using slot generators."""

    slots: dict[str, SlotGenerator] = field(default_factory=dict)
    person_patterns: list[str] = field(default_factory=list)
    place_patterns: list[str] = field(default_factory=list)
    rng: random.Random | None = None

    def _random(self) -> random.Random:
        return self.rng if self.rng is not None else random._inst

    def generate_person(self, pattern: str | None = None) -> str:
        if pattern is None:
            if not self.person_patterns:
                raise ValueError("No person patterns configured")
            pattern = self._random().choice(self.person_patterns)
        return self._fill(pattern)

    def generate_place(self, pattern: str | None = None) -> str:
        if pattern is None:
            if not self.place_patterns:
                raise ValueError("No place patterns configured")
            pattern = self._random().choice(self.place_patterns)
        return self._fill(pattern)

    def _fill(self, pattern: str) -> str:
        slot_map = _SlotMap(self.slots)
        result = pattern.format_map(slot_map)
        return _titlecase_name(result)


def translate_word_list(word_list: list[str], dictionary: dict[str, str]) -> list[str]:
    """Replace words in word_list using dictionary, passing through unknowns."""
    return [dictionary.get(word, word) for word in word_list]


def _longest_common_substring(a: str, b: str) -> int:
    """Return the length of the longest common substring of ``a`` and ``b``.

    Rolling 1-D DP over the standard m×n table. Runs at O(m·n) time
    and O(min(m, n)) space; both name tokens are bounded by the
    Markov chain's ``max_length`` (12 chars) so the absolute cost is
    trivial — but the rolling form keeps the algorithm presentable.
    """
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    longest = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > longest:
                    longest = curr[j]
        prev = curr
    return longest


def has_stem_collision(name: str) -> bool:
    """Return True if ``name`` exhibits the "Frandrew Andrew" stem-repetition pattern.

    Operational definition (per ``sprint/context/context-story-45-28.md``):
    strip whitespace, case-fold each space-separated token, compute the
    longest common substring of length ≥ ``STEM_OVERLAP_MIN``. If the
    substring spans more than half of *either* token, the name is a
    collision and should be rejected.

    Tokens shorter than ``STEM_OVERLAP_MIN`` cannot contribute a
    qualifying overlap (their LCS with anything is bounded by their
    own length); we skip them so connector words like ``de`` / ``of``
    / ``the`` never cause false collisions.

    The predicate operates on tokens *within one name*, never across
    separate names — culturally-coherent stem reuse like Vaal-Kesh /
    Vaal-Tor is intentional morphological coherence (ADR-043), not a
    bug.
    """
    tokens = [t.casefold() for t in name.split() if len(t) >= STEM_OVERLAP_MIN]
    for i, t1 in enumerate(tokens):
        for t2 in tokens[i + 1 :]:
            lcs = _longest_common_substring(t1, t2)
            if lcs < STEM_OVERLAP_MIN:
                continue
            if lcs / len(t1) > 0.5 or lcs / len(t2) > 0.5:
                return True
    return False


def _titlecase_name(name: str) -> str:
    """Title-case a name, keeping small words lowercase."""
    small_words = {"de", "of", "the", "and", "le", "la", "von", "van", "du", "des"}
    parts = name.split()
    result = []
    for i, part in enumerate(parts):
        if i == 0 or part.lower() not in small_words:
            result.append(part.capitalize())
        else:
            result.append(part.lower())
    return " ".join(result)


def _check_corpus_size(
    *,
    text: str,
    corpus_name: str,
    culture_name: str,
    slot_name: str,
) -> None:
    """Apply the namegen min-pool guard (Story 45-28).

    Called once per corpus cache miss (before training). Below
    ``FAIL_BELOW_WORDS`` the corpus cannot produce non-degenerate
    Markov output — emit ``namegen.fail_loud`` and raise so the
    caller fails loud. Below ``WARN_BELOW_WORDS`` the chain still
    works but is at risk of stem-repetition artifacts — emit
    ``namegen.thin_corpus`` and log a warning so Sebastien (GM panel)
    and the operator (stderr) both see the signal.
    """
    word_count = count_words(text)
    if word_count < FAIL_BELOW_WORDS:
        with Span.open(
            SPAN_NAMEGEN_FAIL_LOUD,
            {
                "corpus_name": corpus_name,
                "word_count": word_count,
                "culture": culture_name,
                "slot_name": slot_name,
                "reason": "below_floor",
            },
        ):
            pass
        raise ValueError(
            f"Corpus '{corpus_name}' has {word_count} words; minimum is {FAIL_BELOW_WORDS}"
        )
    if word_count < WARN_BELOW_WORDS:
        with Span.open(
            SPAN_NAMEGEN_THIN_CORPUS,
            {
                "corpus_name": corpus_name,
                "word_count": word_count,
                "culture": culture_name,
                "slot_name": slot_name,
                "threshold": WARN_BELOW_WORDS,
            },
        ):
            pass
        logger.warning(
            "namegen: corpus %s for culture %s slot %s has %d words "
            "(threshold %d); chain may produce stem-repetition artifacts",
            corpus_name,
            culture_name,
            slot_name,
            word_count,
            WARN_BELOW_WORDS,
        )


def _resolve_corpus_file(filename: str, corpus_dir: Path, fallback_dirs: list[Path]) -> Path:
    """Find a corpus file in corpus_dir or fallback directories."""
    primary = corpus_dir / filename
    if primary.exists():
        return primary
    for fdir in fallback_dirs:
        candidate = fdir / filename
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Corpus file '{filename}' not found in {corpus_dir} or fallback dirs {fallback_dirs}"
    )


def build_from_culture(
    culture: Culture,
    corpus_dir: Path,
    rng: random.Random | None = None,
    chain_cache: dict[tuple[str, int], str] | None = None,
    fallback_dirs: list[Path] | None = None,
) -> NameGenerator:
    """Build a NameGenerator from a Culture and corpus directory.

    All corpora for a slot are trained into a single MarkovChain so that
    character transitions blend at the phonemic level. This produces words
    that don't exist in either source language.

    Args:
        culture: A Culture model instance (from genre pack models).
        corpus_dir: Path to the genre pack's corpus/ directory.
        rng: Optional RNG for deterministic output.
        chain_cache: Optional cache of raw corpus text keyed by
                     (corpus_filename, lookback).
        fallback_dirs: Additional directories to search when a corpus
                       file is not found in corpus_dir (e.g. the
                       centralized sidequest-content/corpus/shared/).
    """
    if chain_cache is None:
        chain_cache = {}
    _fallbacks = fallback_dirs or []

    slots: dict[str, SlotGenerator] = {}

    for slot_name, slot_config in culture.slots.items():
        chain: MarkovChain | None = None
        lookback = slot_config.lookback if slot_config.lookback is not None else 2

        # Build local word list (never mutate the shared pydantic model).
        word_list: list[str] = list(slot_config.word_list or [])

        if slot_config.names_file:
            names_path = _resolve_corpus_file(slot_config.names_file, corpus_dir, _fallbacks)
            word_list = [
                line.strip()
                for line in names_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        if slot_config.corpora:
            chain = MarkovChain(lookback=lookback, rng=rng)

            for corpus_ref in slot_config.corpora:
                corpus_path = _resolve_corpus_file(corpus_ref.corpus, corpus_dir, _fallbacks)

                cache_key = (corpus_ref.corpus, lookback)
                if cache_key not in chain_cache:
                    text = corpus_path.read_text(encoding="utf-8")
                    _check_corpus_size(
                        text=text,
                        corpus_name=corpus_ref.corpus,
                        culture_name=culture.name,
                        slot_name=slot_name,
                    )
                    chain_cache[cache_key] = text

                text = chain_cache[cache_key]
                rounds = max(1, round(corpus_ref.weight))
                for _ in range(rounds):
                    chain.train_file(text)

            for reject_file in slot_config.reject_files:
                reject_path = corpus_dir / reject_file
                if not reject_path.exists():
                    for fdir in _fallbacks:
                        candidate = fdir / reject_file
                        if candidate.exists():
                            reject_path = candidate
                            break
                if reject_path.exists():
                    chain.load_reject_file(reject_path)

        # If the slot has both a word_list and corpora, translate the word list
        # into fantasy equivalents using the trained chain.
        if word_list and chain is not None:
            generated = generate_dictionary(chain, word_list)
            word_list = translate_word_list(word_list, generated)

        slots[slot_name] = SlotGenerator(
            chain=chain,
            word_list=word_list,
            rng=rng,
        )

    return NameGenerator(
        slots=slots,
        person_patterns=list(culture.person_patterns),
        place_patterns=list(culture.place_patterns),
        rng=rng,
    )
