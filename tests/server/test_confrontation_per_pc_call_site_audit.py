"""Call-site audit — all 5 build_confrontation_payload sites must project per-PC.

Story 49-7 RED. The story body lists exactly the dispatch sites that
broadcast a CONFRONTATION today and warns:

    All five build sites must move to per-recipient (audit with grep
    before declaring done — ``build_confrontation_payload\\|
    build_clear_confrontation_payload``).

This is the audit test. For each of the five production sites, we
ast-parse the source file and confirm that every
``build_confrontation_payload(...)`` call (other than the definition
itself) is either:

  a. Inside a ``for ... in ... connected_player_ids(...)`` loop with the
     per-recipient class/slots/prepared context resolved, OR
  b. Passes a ``recipient_pc=`` keyword argument directly (the
     single-recipient sites like slug-resume and yield).

The asymmetric ``build_clear_confrontation_payload`` (empty beats) is
intentionally NOT audited — story note in the session:
``build_clear_confrontation_payload is empty-beats already — keep
single broadcast for clear since there is nothing to per-PC project.``

If a new call site is added in the future and forgotten here, the test
will still fail when the audit's enumerated path list goes stale —
catching the regression Sebastien's GM panel would otherwise miss.

This is the wiring test the per-pc projection suite needs per CLAUDE.md
('Every Test Suite Needs a Wiring Test' / 'Verify Wiring, Not Just
Existence').
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Repo root: tests/server/this_file -> tests -> sidequest-server
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The five production sites that build a per-PC CONFRONTATION payload.
# The story body enumerates four explicitly and warns 'Fifth site TBD
# during red phase' — the fifth is the slug-resume bootstrap in
# handlers/connect.py:1110 (re-emits CONFRONTATION when the client
# reloads a tab mid-encounter). Source grep:
#   grep -rn 'build_confrontation_payload\b' sidequest/
PER_PC_SITES: list[tuple[str, str]] = [
    (
        "sidequest/server/websocket_session_handler.py",
        "post-narration broadcast (was: single _emit_event with full beats)",
    ),
    (
        "sidequest/server/dispatch/dice.py",
        "mid-turn post-dice broadcast (Story 45-3 momentum sync)",
    ),
    (
        "sidequest/handlers/yield_action.py",
        "partial yield CONFRONTATION sent to yielding player",
    ),
    (
        "sidequest/handlers/connect.py",
        "slug-resume bootstrap CONFRONTATION sent to resuming player",
    ),
]


def _parse(source_path: Path) -> ast.AST:
    return ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))


def _build_confrontation_calls(tree: ast.AST) -> list[ast.Call]:
    """Collect every ``build_confrontation_payload(...)`` call in the tree.

    Both direct-import (``build_confrontation_payload(encounter=...)``)
    and attribute-style (``confrontation.build_confrontation_payload(...)``)
    are recognized.
    """
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == "build_confrontation_payload") or (
            isinstance(func, ast.Attribute) and func.attr == "build_confrontation_payload"
        ):
            calls.append(node)
    return calls


def _enclosing_for_loops(tree: ast.AST, target: ast.AST) -> list[ast.For]:
    """Return the chain of ``for`` loops that lexically enclose ``target``."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    enclosing: list[ast.For] = []
    cursor: ast.AST | None = target
    while cursor is not None:
        cursor = parents.get(id(cursor))
        if isinstance(cursor, ast.For):
            enclosing.append(cursor)
    return enclosing


def _iter_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "<unparse-failed>"


def _call_has_recipient_pc_keyword(call: ast.Call) -> bool:
    return any(kw.arg == "recipient_pc" for kw in call.keywords)


def _loop_iter_calls_connected_player_ids(loop: ast.For) -> bool:
    """Does ``for X in <expr>:`` iterate over ``...connected_player_ids(...)``?"""
    for sub in ast.walk(loop.iter):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "connected_player_ids"
        ):
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "connected_player_ids":
            return True
    return False


@pytest.mark.parametrize(("rel_path", "description"), PER_PC_SITES)
def test_call_site_passes_recipient_pc_or_iterates_recipients(
    rel_path: str,
    description: str,
) -> None:
    """Every per-PC site must either pass ``recipient_pc=`` directly OR
    sit inside a ``for ... connected_player_ids ...:`` loop with a
    per-recipient ``recipient_pc=`` build call inside.

    Pre-fix every site fails: each call shape is
    ``build_confrontation_payload(encounter=..., cdef=..., genre_slug=...)``
    with no recipient_pc keyword and no enclosing connected-player loop.
    """
    src = _REPO_ROOT / rel_path
    assert src.is_file(), f"audit list out of date: {src} no longer exists"
    tree = _parse(src)
    calls = _build_confrontation_calls(tree)
    assert calls, (
        f"audit list out of date: no build_confrontation_payload calls found in "
        f"{rel_path} ({description})"
    )

    failures: list[str] = []
    for call in calls:
        if _call_has_recipient_pc_keyword(call):
            continue
        loops = _enclosing_for_loops(tree, call)
        if any(_loop_iter_calls_connected_player_ids(loop) for loop in loops):
            # The call sits inside a connected_player_ids loop — the
            # recipient_pc keyword may be constructed and forwarded
            # within the loop body. This counts as wired so long as the
            # loop also produces a recipient_pc-carrying call SOMEWHERE
            # in its body.
            wired = any(
                _call_has_recipient_pc_keyword(inner)
                for loop in loops
                for inner in _build_confrontation_calls(loop)
            )
            if wired:
                continue
        failures.append(f"  line {call.lineno}: {_iter_unparse(call)}")

    assert not failures, (
        f"{rel_path} ({description}) has build_confrontation_payload call(s) "
        f"that do not pass recipient_pc and are not inside a per-recipient "
        f"connected_player_ids loop:\n" + "\n".join(failures)
    )


def test_clear_payload_remains_single_broadcast_in_websocket_handler() -> None:
    """The asymmetry must be documented and preserved:
    ``build_clear_confrontation_payload`` (empty beats) stays a single
    broadcast. If a future refactor symmetry-collapses the two payload
    builders into a single per-recipient loop, the clear emit will be
    needlessly multiplied N times — micro-cost but wrong on shape.
    """
    src = _REPO_ROOT / "sidequest/server/websocket_session_handler.py"
    tree = _parse(src)
    clear_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == "build_clear_confrontation_payload"
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "build_clear_confrontation_payload"
            )
        )
    ]
    assert clear_calls, "audit out of date: no build_clear_confrontation_payload calls in handler"
    for call in clear_calls:
        loops = _enclosing_for_loops(tree, call)
        bad = [loop for loop in loops if _loop_iter_calls_connected_player_ids(loop)]
        assert not bad, (
            f"build_clear_confrontation_payload at line {call.lineno} sits inside "
            f"a connected_player_ids loop — clear payload should be a single "
            f"broadcast (empty beats == nothing to per-PC project)."
        )


def test_audit_list_covers_every_build_confrontation_payload_call_site() -> None:
    """Drift guard: if a new caller of build_confrontation_payload is
    added to ``sidequest/`` and forgotten here, this test fails so the
    audit list above is updated. The definition file itself is
    intentionally excluded — it contains the function declaration, not
    a call site.
    """
    server_root = _REPO_ROOT / "sidequest"
    definition_file = (_REPO_ROOT / "sidequest/server/dispatch/confrontation.py").resolve()
    found: list[str] = []
    for path in server_root.rglob("*.py"):
        if path.resolve() == definition_file:
            continue
        try:
            tree = _parse(path)
        except SyntaxError:
            continue
        if _build_confrontation_calls(tree):
            found.append(str(path.relative_to(_REPO_ROOT)))

    audited = {rel for rel, _ in PER_PC_SITES}
    unaudited = sorted(set(found) - audited)
    assert not unaudited, (
        f"build_confrontation_payload is called from {unaudited!r} which is "
        f"not in PER_PC_SITES — add it to the audit list and ensure it projects "
        f"per-recipient."
    )
