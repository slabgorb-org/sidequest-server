"""Story 122-5 — CI guard: the ADR-147 layering law, enforced.

ADR-147 ("Honest Layering") establishes one import-direction law:

    Imports flow downward only:
        foundation <- {game, genre, orbital, magic, interior} <- server

Domain and utility ("foundation") code MUST NOT import from ``sidequest.server``
— neither at module load nor via a lazy in-method import. ``server/`` may import
anything below it; nothing below it may import upward.

This file is the *enforcing* final step (122-5) of epic 122. Stories 122-1..122-4
relocated the misfiled units that forced upward edges:

    122-1  foundation floor — asset_urls / slug_fold / reference_anchors moved down
    122-2  pure combat-rules helpers moved into game/ruleset (lazy imports deleted)
    122-3  interior HTTP endpoint lifted to server/rest.py (interior/ left pure)
    122-4  orbital/intent narrowed from Session to a read Protocol

This guard fails the build if any *new* upward edge creeps back in. It is the
wiring test for layer direction (project rule: every suite needs a wiring test)
and the executable form of ADR-147 §Enforcement — "a ~20-line AST/grep test in
tests/ is sufficient (no import-linter dependency), consistent with ADR-088's
'the script is the schema' stance."

The scan uses ``ast.walk`` so lazy in-method imports — the exact dodge ADR-147
calls out in native.py / without_number.py / projection/validator.py — are
caught alongside top-level ones.

GRANDFATHERED EXCEPTION (loud, pinned, and self-expiring — NOT a silent fallback)
--------------------------------------------------------------------------------
``sidequest/game/projection/validator.py`` reaches up into
``sidequest.server.session_handler`` for ``_KIND_TO_MESSAGE_CLS`` via two lazy
in-method imports. ADR-147 NAMES this edge in its diagnosis (the "smell that
proves the layering is dishonest" paragraph) but deliberately did NOT schedule a
move for it — the §Decision moves table and the 5-step §Implementation Plan
cover only the six relocated units, none of which is this one. Relocating
``_KIND_TO_MESSAGE_CLS`` (defined in ``server/session_handler.py``, also consumed
by ``server/emitters.py``) is genuine design work outside this 2pt guard story
and outside ADR-147's sanctioned scope.

So the guard grandfathers exactly this one (file, target) pair: it lands
ENFORCING against every *other* edge today rather than deferring the whole guard
to ``xfail``. The exception is pinned to exact import targets — a new offending
import elsewhere, or validator.py importing a *different* server symbol, fails
``test_no_upward_imports_beyond_grandfathered``. And
``test_grandfathered_exceptions_are_still_live`` fails the moment the edge is
removed, forcing this exception to be DELETED rather than lingering forever. See
the Conflict delivery finding (122-5) recommending a follow-up to move
``_KIND_TO_MESSAGE_CLS`` down to the protocol tier, after which this whole block
goes away.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import sidequest  # noqa: E402  (path discovery needs the import side effect)

# --- Package geography -------------------------------------------------------

SIDEQUEST_PKG: Path = Path(sidequest.__file__).resolve().parent

# The tiers ADR-147 §Enforcement names: the foundation floor plus the five
# domain packages. None of these may import upward into ``sidequest.server``.
GUARDED_TIERS: tuple[str, ...] = (
    "foundation",
    "game",
    "genre",
    "orbital",
    "magic",
    "interior",
)

# The one upward edge ADR-147 acknowledges but does not schedule a move for.
# Keyed by package-relative posix path; value is the exact set of server import
# targets that file is permitted to reference. Anything outside this set — in
# this file or any other — is a violation. See the module docstring.
GRANDFATHERED: dict[str, frozenset[str]] = {
    "game/projection/validator.py": frozenset(
        {
            # `from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS`
            # contributes both the module path and the imported-name path.
            "sidequest.server.session_handler",
            "sidequest.server.session_handler._KIND_TO_MESSAGE_CLS",
        }
    ),
}


# --- AST import scanning -----------------------------------------------------


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _server_import_targets(tree: ast.AST) -> set[str]:
    """Every ``sidequest.server`` dotted path this AST imports.

    ``ast.walk`` recurses into function/method bodies, so lazy in-method imports
    are caught alongside module-level ones. Both spellings are detected:
    ``from sidequest.server import x`` (module ``sidequest.server`` + name
    ``sidequest.server.x``) and ``import sidequest.server.x`` (alias name).
    """
    targets: set[str] = set()

    def _record(dotted: str) -> None:
        if dotted == "sidequest.server" or dotted.startswith("sidequest.server."):
            targets.add(dotted)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            _record(mod)
            for alias in node.names:
                _record(f"{mod}.{alias.name}" if mod else alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                _record(alias.name)
    return targets


def _scan_tier(tier: str) -> dict[str, set[str]]:
    """Map {package-relative-posix-path: {server import targets}} for one tier."""
    tier_dir = SIDEQUEST_PKG / tier
    if not tier_dir.is_dir():
        return {}
    found: dict[str, set[str]] = {}
    for path in _iter_py_files(tier_dir):
        targets = _server_import_targets(ast.parse(path.read_text(encoding="utf-8")))
        if targets:
            found[path.relative_to(SIDEQUEST_PKG).as_posix()] = targets
    return found


def _all_server_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for tier in GUARDED_TIERS:
        found.update(_scan_tier(tier))
    return found


# ---------------------------------------------------------------------------
# Coverage guard: the scan must not be vacuous.
# ---------------------------------------------------------------------------


def test_guarded_tiers_exist_and_have_modules() -> None:
    """Every guarded tier must exist and contain Python modules.

    Without this, a renamed/removed package would make the upward-import scan
    silently pass over nothing (a false green). Fail loud instead.
    """
    missing = [t for t in GUARDED_TIERS if not (SIDEQUEST_PKG / t).is_dir()]
    assert not missing, f"ADR-147 guarded tiers missing from sidequest/: {missing}"
    empty = [t for t in GUARDED_TIERS if not _iter_py_files(SIDEQUEST_PKG / t)]
    assert not empty, f"ADR-147 guarded tiers contain no .py files: {empty}"


# ---------------------------------------------------------------------------
# The law: no upward imports beyond the documented grandfather set.
# ---------------------------------------------------------------------------


def test_no_upward_imports_beyond_grandfathered() -> None:
    violations: dict[str, list[str]] = {}
    for rel_path, targets in _all_server_imports().items():
        allowed = GRANDFATHERED.get(rel_path, frozenset())
        offending = sorted(targets - allowed)
        if offending:
            violations[rel_path] = offending

    assert violations == {}, (
        "ADR-147 layering law violated — domain/foundation code imports UP into "
        "sidequest.server. A unit belongs in the lowest tier its dependencies "
        "permit; move the imported symbol down (see ADR-147 §The moves) rather "
        "than reaching up. Upward edges:\n"
        + "\n".join(f"  {f}: {hits}" for f, hits in sorted(violations.items()))
    )


# ---------------------------------------------------------------------------
# Self-expiry: a grandfathered exception that is no longer needed must be
# deleted, not left to rot. If validator.py stops importing the pinned target,
# this fails — forcing removal of the exception (and this whole block).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", sorted(GRANDFATHERED))
def test_grandfathered_exceptions_are_still_live(rel_path: str) -> None:
    path = SIDEQUEST_PKG / rel_path
    assert path.exists(), (
        f"Grandfathered file {rel_path} no longer exists — delete its entry "
        "from GRANDFATHERED (the edge it covered is gone)."
    )
    actual = _server_import_targets(ast.parse(path.read_text(encoding="utf-8")))
    expected = GRANDFATHERED[rel_path]
    stale = sorted(expected - actual)
    assert not stale, (
        f"{rel_path} no longer imports {stale} from sidequest.server — the "
        "ADR-147 grandfather exception is stale. DELETE the now-unnecessary "
        "entry from GRANDFATHERED so the guard tightens automatically."
    )
