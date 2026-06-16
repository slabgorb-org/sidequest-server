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
caught alongside top-level ones. Relative imports (``from ...server import x``,
``from .. import server``) are resolved to their absolute path against the
importing module's package before matching, so they cannot evade the guard by
switching spelling. Dynamic imports (``importlib.import_module``/``__import__``)
are ``ast.Call`` nodes, not import statements, and are a documented
out-of-scope limitation — see ``_server_import_targets``.

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


def _package_parts_for(path: Path) -> list[str]:
    """Dotted parts of the *containing package* of a module file.

    Needed to resolve relative imports to absolute paths. Examples:
      sidequest/game/projection/validator.py -> ['sidequest','game','projection']
      sidequest/game/__init__.py             -> ['sidequest','game']

    The module is anchored at ``sidequest`` (``SIDEQUEST_PKG.parent`` is its
    parent on disk), so the path relative to that parent, minus the module name,
    is the package. ``__init__.py`` is the package itself; dropping the final
    component handles both cases identically.
    """
    rel = path.relative_to(SIDEQUEST_PKG.parent).with_suffix("")
    return list(rel.parts)[:-1]


def _resolve_relative(level: int, module: str | None, package_parts: list[str]) -> str | None:
    """Resolve a relative ``ImportFrom`` to its absolute dotted module path.

    ``level`` is the number of leading dots; ``level==1`` is the current package,
    each extra dot strips one trailing package component. Returns ``None`` for an
    over-deep relative import (one Python itself would reject at import time).
    """
    drop = level - 1
    if drop > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - drop]
    suffix = module.split(".") if module else []
    return ".".join(base + suffix)


def _server_import_targets(tree: ast.AST, package_parts: list[str]) -> set[str]:
    """Every ``sidequest.server`` dotted path this AST imports.

    ``ast.walk`` recurses into function/method bodies, so lazy in-method imports
    are caught alongside module-level ones. All static import spellings resolve
    to an absolute path before matching:

      * ``from sidequest.server import x``       -> sidequest.server[.x]
      * ``import sidequest.server.x [as s]``     -> sidequest.server.x
      * ``from sidequest import server``         -> sidequest.server
      * ``from ...server import x`` (relative)   -> resolved via ``package_parts``
      * ``from .. import server``   (relative)   -> resolved via ``package_parts``

    ``package_parts`` is the importing module's containing package, used to turn
    relative imports into absolute paths. Relative imports that resolve to a
    *sibling* (e.g. ``from ..server`` inside ``game/projection`` -> the
    nonexistent ``sidequest.game.server``) correctly do NOT match.

    KNOWN LIMITATION: dynamic imports — ``importlib.import_module("sidequest.server")``
    and ``__import__("sidequest.server")`` — are ``ast.Call`` nodes, not import
    statements, and are out of scope for this static scan. Domain code reaching
    server via runtime string imports is not prevented here (it would also defeat
    the codebase's absolute-import convention and stand out in review).
    """
    targets: set[str] = set()

    def _record(dotted: str | None) -> None:
        if dotted and (dotted == "sidequest.server" or dotted.startswith("sidequest.server.")):
            targets.add(dotted)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolve against the file's package
                mod = _resolve_relative(node.level, node.module, package_parts)
            else:
                mod = node.module
            _record(mod)
            for alias in node.names:
                _record(f"{mod}.{alias.name}" if mod else None)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                _record(alias.name)
    return targets


def _parse_module(path: Path) -> ast.AST:
    """Read + parse a module, failing loud (not crashing) on bad input.

    A malformed or non-UTF-8 ``.py`` file under a guarded tier must produce a
    clear, actionable failure naming the file — not an opaque ``SyntaxError`` /
    ``UnicodeDecodeError`` traceback unrelated to the layering law (No Silent
    Fallbacks: fail loud *and* legibly).
    """
    rel = path.relative_to(SIDEQUEST_PKG).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"import-direction guard: {rel} is not valid UTF-8 ({exc}).")
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        pytest.fail(f"import-direction guard: {rel} failed to parse ({exc}).")


def _scan_tier(tier: str) -> dict[str, set[str]]:
    """Map {package-relative-posix-path: {server import targets}} for one tier."""
    tier_dir = SIDEQUEST_PKG / tier
    if not tier_dir.is_dir():
        return {}
    found: dict[str, set[str]] = {}
    for path in _iter_py_files(tier_dir):
        targets = _server_import_targets(_parse_module(path), _package_parts_for(path))
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
    actual = _server_import_targets(_parse_module(path), _package_parts_for(path))
    expected = GRANDFATHERED[rel_path]
    stale = sorted(expected - actual)
    assert not stale, (
        f"{rel_path} no longer imports {stale} from sidequest.server — the "
        "ADR-147 grandfather exception is stale. DELETE the now-unnecessary "
        "entry from GRANDFATHERED so the guard tightens automatically."
    )


# ---------------------------------------------------------------------------
# Resolver unit tests: relative-import spellings must resolve correctly.
# (Pure-logic tests on synthetic ASTs — we cannot add a real relative
# import-to-server file without it being an actual layering violation.)
# ---------------------------------------------------------------------------


def test_relative_import_reaching_server_is_detected() -> None:
    # A module at sidequest/game/projection/foo.py: `from ...server import x`
    # resolves to sidequest.server and MUST be caught.
    tree = ast.parse("from ...server import session_handler\n")
    pkg = ["sidequest", "game", "projection"]
    targets = _server_import_targets(tree, pkg)
    assert targets == {"sidequest.server", "sidequest.server.session_handler"}


def test_relative_bare_import_reaching_server_is_detected() -> None:
    # `from .. import server` in sidequest/game/foo.py -> sidequest.server.
    tree = ast.parse("from .. import server\n")
    pkg = ["sidequest", "game"]
    targets = _server_import_targets(tree, pkg)
    assert targets == {"sidequest.server"}


def test_relative_import_to_sibling_is_not_a_false_positive() -> None:
    # `from ..server import x` in sidequest/game/projection/foo.py resolves to
    # the sibling sidequest.game.server, NOT sidequest.server — must NOT match.
    tree = ast.parse("from ..server import x\n")
    pkg = ["sidequest", "game", "projection"]
    assert _server_import_targets(tree, pkg) == set()


def test_overdeep_relative_import_does_not_crash() -> None:
    # More dots than package depth: resolve to None (Python would reject it too);
    # the scan must tolerate it without raising.
    tree = ast.parse("from ....server import x\n")
    pkg = ["sidequest", "game"]
    assert _server_import_targets(tree, pkg) == set()


def test_package_parts_for_handles_init_and_module() -> None:
    assert _package_parts_for(SIDEQUEST_PKG / "game" / "projection" / "validator.py") == [
        "sidequest",
        "game",
        "projection",
    ]
    assert _package_parts_for(SIDEQUEST_PKG / "game" / "__init__.py") == ["sidequest", "game"]
