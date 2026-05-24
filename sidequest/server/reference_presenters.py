"""Per-section reference-page presenters.

Each presenter is a pure function: (node, PresenterContext) -> str (HTML).
The dispatcher in reference_renderer.py looks up (file_stem, key_path) in
PRESENTERS before falling back to the generic <h2>key</h2><p>value</p> loop.

Wildcard rule: ('*',) in the registry key matches any list-of-dict item slot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sidequest.server.reference_theme import ReferenceTheme

KeyPath = tuple[str, ...]


@dataclass(frozen=True)
class PresenterContext:
    pack: str
    world: str | None
    file_stem: str
    key_path: KeyPath
    theme: ReferenceTheme
    depth: int


Presenter = Callable[[object, PresenterContext], str]


# Registry populated by subsequent tasks. Empty at Task 4 — dispatcher falls
# through to generic for every field, but visibility classification still runs.
PRESENTERS: dict[tuple[str, KeyPath], Presenter] = {}


def lookup_presenter(file_stem: str, key_path: KeyPath) -> Presenter | None:
    """Return the registered presenter or None.

    Exact match only. Callers are responsible for substituting "*" into
    list-of-dict slots in the key_path before calling.
    """
    return PRESENTERS.get((file_stem, key_path))
