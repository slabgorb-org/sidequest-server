"""Regression: ``out`` accumulator must be bound before chargen emit closures fire.

Playtest 2026-05-21 — `Create Character` crashed on every region-mode world
(NavigationMode.region, e.g. tea_and_murder/glenross) with::

    NameError: cannot access free variable 'out' where it is not associated
    with a value in enclosing scope

Root cause: ``_chargen_confirmation`` bound ``out`` mid-method (after the
CharacterCreationMessage was computed), but two nested emit closures
(``_chargen_emit_tactical_grid`` for room_graph, ``_chargen_emit_region_location``
for region) capture ``out`` as a free variable and are invoked on the
location-init seam *before* that binding ran. Region-mode worlds with a
non-empty ``current_region`` hit the region closure → unbound cell → crash,
so no opening narration / LOCATION_DESCRIPTION was ever produced.

The fix binds ``out: list[object] = []`` at the top of the method and
converts the mid-method assignment into ``out.insert(0, ...)`` so the
CharacterCreationMessage stays first while preserving any
LOCATION_DESCRIPTION / TACTICAL_GRID messages the closures already appended.

This is a source-level wiring guard (per the task's accepted fallback): a
true unit call to ``_chargen_confirmation`` would require standing up the
whole materialize → magic → chassis → scenario → persistence pipeline against
live content, which couples the test to prod rows. Instead we assert the
structural invariant that actually broke: ``out`` is bound before BOTH closure
``out.append`` call-sites, and is never re-bound with ``=`` after them.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MODULE = (
    Path(__file__).resolve().parents[2]
    / "sidequest"
    / "server"
    / "websocket_handlers"
    / "chargen_mixin.py"
)


def _chargen_confirmation_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(_MODULE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_chargen_confirmation":
            return node
    raise AssertionError("_chargen_confirmation not found in chargen_mixin.py")


def _is_out_target(target: ast.expr) -> bool:
    return isinstance(target, ast.Name) and target.id == "out"


def test_out_bound_before_emit_closures_append() -> None:
    """``out`` must have a binding line earlier than every ``out.append`` site.

    The append sites live inside the two emit closures, which are invoked
    on the location-init seam. If ``out``'s first binding lands after any of
    them, Python turns ``out`` into an unbound free variable in the closure
    and the region-mode path NameErrors (the exact playtest crash).
    """
    fn = _chargen_confirmation_node()

    # Lines that BIND ``out`` (either annotated `out: ... = ...` or `out = ...`).
    bind_lines: list[int] = []
    # Lines that USE ``out`` via attribute (out.append / out.insert).
    append_lines: list[int] = []

    for node in ast.walk(fn):
        if (isinstance(node, ast.AnnAssign) and _is_out_target(node.target)) or (
            isinstance(node, ast.Assign) and any(_is_out_target(t) for t in node.targets)
        ):
            bind_lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "out"
            and node.func.attr == "append"
        ):
            append_lines.append(node.lineno)

    assert bind_lines, "_chargen_confirmation never binds `out`"
    assert append_lines, (
        "expected `out.append` call-sites in the emit closures; did the closure shape change?"
    )

    first_bind = min(bind_lines)
    earliest_append = min(append_lines)
    assert first_bind < earliest_append, (
        "`out` must be bound before any emit closure appends to it. "
        f"first `out` binding is at line {first_bind} but the earliest "
        f"`out.append` is at line {earliest_append} — this is the "
        "2026-05-21 region-mode NameError regression."
    )


def test_out_not_rebound_after_closures() -> None:
    """No bare ``out = [...]`` assignment may follow the emit closures.

    The original bug was a re-bind (``out: list[object] = [Character...]``)
    that landed after the closures. Re-introducing any ``out = ...`` (vs an
    in-place ``out.insert`` / ``out.append``) after the earliest append would
    re-trigger the same free-variable defect for the closure that ran first.
    """
    fn = _chargen_confirmation_node()

    append_lines: list[int] = []
    rebind_lines: list[int] = []

    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "out"
            and node.func.attr == "append"
        ):
            append_lines.append(node.lineno)
        elif (isinstance(node, ast.AnnAssign) and _is_out_target(node.target)) or (
            isinstance(node, ast.Assign) and any(_is_out_target(t) for t in node.targets)
        ):
            rebind_lines.append(node.lineno)

    assert append_lines, "expected `out.append` call-sites"
    # The single legitimate binding is the top-of-method `out = []`, which is
    # before every append. Any binding AFTER the earliest append is the bug.
    earliest_append = min(append_lines)
    late_rebinds = [ln for ln in rebind_lines if ln > earliest_append]
    assert not late_rebinds, (
        "`out` is re-bound with `=` after an emit closure already appended to "
        f"it (lines {late_rebinds}). Use an in-place op (out.insert/out.append) "
        "instead — re-binding re-triggers the region-mode NameError."
    )


def test_chargen_confirmation_module_imports() -> None:
    """Smoke: the handler module imports cleanly (no syntax/binding error)."""
    import sidequest.server.session_handler  # noqa: F401 — import-order side effect
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

    assert hasattr(WebSocketSessionHandler, "_chargen_confirmation")
