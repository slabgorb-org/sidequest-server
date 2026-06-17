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

ZERO GRANDFATHERED EXCEPTIONS (as of story 122-8)
-------------------------------------------------
The guard once grandfathered a single edge: ``game/projection/validator.py``
reached up into ``server.session_handler`` for ``_KIND_TO_MESSAGE_CLS`` via two
lazy in-method imports. Story 122-8 relocated that registry down to the protocol
tier (``sidequest.protocol.messages``) — the layer-honest home, since protocol
already owns every message class it maps to — so validator now imports it
from below with no upward edge. ``GRANDFATHERED`` is consequently empty and the
law is enforced with no exceptions. The dict is retained (empty) so a future,
genuinely-unavoidable edge can be pinned loudly rather than silenced.
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

# Upward edges the guard permits, keyed by package-relative posix path; value is
# the exact set of server import targets that file may reference. EMPTY as of
# story 122-8 — the last edge (validator.py → session_handler._KIND_TO_MESSAGE_CLS)
# was eliminated by relocating the registry to the protocol tier. Anything that
# imports up into sidequest.server is now a violation. See the module docstring.
GRANDFATHERED: dict[str, frozenset[str]] = {}


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

    The valid floor is the top-level package itself: the deepest legal drop leaves
    ``package_parts[:1]`` (just ``sidequest``). Dropping the whole list
    (``drop >= len(package_parts)``) escapes *above* ``sidequest`` and must resolve
    to ``None``. The boundary is ``>=``, not ``>``: at ``drop == len`` the slice
    ``package_parts[:0]`` is empty, which the old ``drop > len`` guard let through
    and joined into a misleading bare module name (e.g. ``"server"``) instead of
    rejecting the root escape.
    """
    drop = level - 1
    if drop >= len(package_parts):
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

    A malformed, non-UTF-8, or unreadable ``.py`` file under a guarded tier must
    produce a clear, actionable failure naming the file — not an opaque
    ``SyntaxError`` / ``UnicodeDecodeError`` / ``OSError`` traceback unrelated to
    the layering law (No Silent Fallbacks: fail loud *and* legibly). ``OSError``
    covers a file that vanished between glob and read, a permission error, or a
    path that is unexpectedly a directory — none of which should crash the whole
    suite opaquely.
    """
    rel = path.relative_to(SIDEQUEST_PKG).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"import-direction guard: {rel} is not valid UTF-8 ({exc}).")
    except OSError as exc:
        pytest.fail(f"import-direction guard: {rel} could not be read ({exc}).")
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


def test_sidequest_pkg_is_the_real_package() -> None:
    """``SIDEQUEST_PKG`` must be the actual importable ``sidequest`` package root.

    Identity check: every scan (``_scan_tier``, ``_package_parts_for``) is anchored
    at ``SIDEQUEST_PKG``. If that root ever resolved to a stray directory or a
    namespace-package shadow, the whole guard would run against the wrong tree and
    pass vacuously. Pin it to the regular package whose ``__init__.py`` Python
    actually imported as ``sidequest``.
    """
    assert SIDEQUEST_PKG.name == "sidequest", (
        f"import-direction guard anchored at the wrong root: {SIDEQUEST_PKG}"
    )
    assert (SIDEQUEST_PKG / "__init__.py").is_file(), (
        f"{SIDEQUEST_PKG} is not a regular package (no __init__.py) — guard root is wrong"
    )
    assert Path(sidequest.__file__).resolve() == SIDEQUEST_PKG / "__init__.py", (
        "SIDEQUEST_PKG does not match the location Python imported `sidequest` from "
        "— a shadowing package may be on the path"
    )


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


def test_resolve_relative_rejects_root_escape_at_boundary() -> None:
    # drop == len(package_parts) escapes ABOVE the `sidequest` top-level and must
    # resolve to None. `from ...server import x` inside sidequest/game/ has
    # level=3 -> drop=2 == len(["sidequest","game"]). The pre-122-7 `drop > len`
    # boundary wrongly let this through as an empty base joined to a bare
    # "server"; the `drop >= len` boundary rejects it.
    assert _resolve_relative(3, "server", ["sidequest", "game"]) is None
    tree = ast.parse("from ...server import session_handler\n")
    assert _server_import_targets(tree, ["sidequest", "game"]) == set()


def test_resolve_relative_keeps_deepest_valid_drop() -> None:
    # The boundary must not over-tighten: dropping len-1 components leaves the
    # top-level `sidequest` package, which is valid. `from .. import server`
    # inside sidequest/game/ resolves to sidequest.server.
    assert _resolve_relative(2, None, ["sidequest", "game"]) == "sidequest"


def test_parse_module_fails_loud_on_unreadable_file() -> None:
    # An OSError on read (vanished file, permission denied, path-is-a-directory)
    # must become a clear, attributable guard failure naming the file — not an
    # opaque traceback (No Silent Fallbacks).
    probe = SIDEQUEST_PKG / "game" / "does_not_exist_122_7_guard_probe.py"
    with pytest.raises(pytest.fail.Exception, match="could not be read"):
        _parse_module(probe)


def test_package_parts_for_handles_init_and_module() -> None:
    assert _package_parts_for(SIDEQUEST_PKG / "game" / "projection" / "validator.py") == [
        "sidequest",
        "game",
        "projection",
    ]
    assert _package_parts_for(SIDEQUEST_PKG / "game" / "__init__.py") == ["sidequest", "game"]
