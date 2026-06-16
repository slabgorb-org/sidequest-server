from __future__ import annotations

from collections.abc import Iterable, Iterator

from sidequest.corpus.schema import TrainingPair

_RETARGET_TOKENS = (
    "no,",
    "i meant",
    "wait",
    "actually",
    "not the",
)


def detect_retarget(pairs: Iterable[TrainingPair]) -> Iterator[TrainingPair]:
    """Emit each pair whose NEXT pair's input contains a retarget token.

    Heuristic: a retarget in turn N+1 suggests turn N's referent was mis-resolved.
    Matching is case-insensitive. Tokens are substrings (not word-boundary) because
    player input is informal and punctuation-loose.
    """
    ordered = list(pairs)
    for i in range(len(ordered) - 1):
        next_lower = ordered[i + 1].input_text.lower()
        if any(tok in next_lower for tok in _RETARGET_TOKENS):
            yield ordered[i]
