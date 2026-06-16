"""Story 122-4 (RED) — Narrow ``handle_orbital_intent`` from the full
``sidequest.server.session.Session`` onto a small read Protocol (ADR-147 step,
epic-122 "Honest Layering").

The test IS the spec. ADR-147's layering law forbids upward edges:

    Imports flow downward only:
        foundation <- {game, genre, orbital, magic, interior} <- server

``sidequest/orbital/intent.py`` is the *last* file in the orbital tier that
reaches up into ``sidequest.server`` (it does
``from sidequest.server.session import Session`` purely for a type hint). This
story kills that edge by narrowing ``handle_orbital_intent``'s parameter to a
Protocol declared *inside* the orbital tier, so the function depends only on the
shape it actually uses — not on the server's concrete ``Session``.

WHAT THE FUNCTION ACTUALLY TOUCHES ON ``session`` (audited 2026-06-15):

    - ``orbital_content``           read   -> OrbitalContent | None
    - ``orbital_scope``             read + WRITE (the drill_out branch reads it;
                                    every branch assigns it back)
    - ``clock``                     read   -> object with ``.t_hours``
    - ``party_body_id``             read   -> str | None
    - ``_snapshot.plotted_course``  read   -> PlottedCourse | None  (a PRIVATE
                                    reach-through into ``session._snapshot``)

That is FIVE members, not "4 fields", one of them read+write, and one of them a
private ``_snapshot`` reach-through. The narrowing therefore CANNOT be a literal
4-field *read* Protocol:

    * ``orbital_scope`` must be a mutable (read+write) member of the Protocol.
    * ``plotted_course`` must be promoted to a clean member of the narrow surface
      (``session.plotted_course``) — a Protocol cannot expose a private
      ``_snapshot``. ``Session`` currently has NO ``plotted_course`` accessor, so
      Dev must add one (returning ``self._snapshot.plotted_course``) to keep the
      production caller — ``handlers/orbital_intent.py`` passing ``room.session``
      — structurally conformant.

INTENDED PUBLIC NAME (chosen by TEA; Dev may rename, but then update the two
name-bound tests below): ``OrbitalIntentSession``, a ``@runtime_checkable``
``typing.Protocol`` exported from ``sidequest.orbital.intent``.

ENFORCED INVARIANTS (each fails crisply until Dev lands the narrowing):

  A. ``OrbitalIntentSession`` exists in ``sidequest.orbital.intent``, is a
     runtime-checkable Protocol, and ``handle_orbital_intent``'s ``session``
     parameter is annotated with it (NOT with ``Session``).
  B. The function runs against a MINIMAL non-``Session`` stand-in exposing only
     the five narrow members — proving it no longer needs the full ``Session``.
  C. The stand-in carries NO ``_snapshot``; touching it trips a tripwire. This
     proves the private ``session._snapshot.plotted_course`` reach-through was
     replaced by the clean ``session.plotted_course`` member.
  D. A real ``Session`` still satisfies the Protocol and drives the function —
     the production caller is not broken (wiring/regression).
  E. ``sidequest/orbital/intent.py`` no longer imports ``sidequest.server`` (the
     killed upward edge), and — because intent.py was the tier's last offender —
     the whole ``sidequest/orbital/`` tier is now server-pure. The AST scan is
     the layer-direction wiring test ADR-147 §Enforcement prescribes; 122-5
     generalises it to a CI guard over all five domain packages.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import sidequest  # noqa: E402  (import side effect needed for path discovery)
from sidequest.game.session import GameSnapshot
from sidequest.orbital.clock import Clock
from sidequest.orbital.course import CourseSource, PlottedCourse
from sidequest.orbital.intent import (
    OrbitalContentUnavailableError,
    handle_orbital_intent,
)
from sidequest.orbital.loader import load_orbital_content
from sidequest.orbital.render import Scope
from sidequest.protocol.orbital_intent import OrbitalIntent, OrbitalIntentResponse
from sidequest.server.session import Session

FIXTURES = Path(__file__).parent / "fixtures"
SIDEQUEST_PKG: Path = Path(sidequest.__file__).resolve().parent


def _view_map(scope: str = "system_root") -> OrbitalIntent:
    return OrbitalIntent.model_validate({"kind": "view_map", "scope": scope})


def _drill_in(body_id: str) -> OrbitalIntent:
    return OrbitalIntent.model_validate({"kind": "drill_in", "body_id": body_id})


def _drill_out() -> OrbitalIntent:
    return OrbitalIntent.model_validate({"kind": "drill_out"})


# ---------------------------------------------------------------------------
# A minimal stand-in implementing ONLY the narrow surface (NOT a Session).
# ---------------------------------------------------------------------------


class _SnapshotTripwire:
    """Sentinel that fails loudly if the narrowed function reaches for
    ``session._snapshot`` (the private reach-through 122-4 must remove)."""

    def __getattr__(self, name: str) -> object:  # pragma: no cover - tripwire
        raise AssertionError(
            "handle_orbital_intent touched session._snapshot — the narrowing "
            "must read session.plotted_course from the Protocol surface instead "
            f"of reaching through the private _snapshot (asked for: {name!r})."
        )


class NarrowOrbitalSession:
    """Duck-typed stand-in exposing exactly what ``handle_orbital_intent`` needs.

    Deliberately NOT a ``Session`` and deliberately WITHOUT a usable
    ``_snapshot`` — if the function accepts this object and produces a correct
    response, it depends only on the narrow shape, not on the server type.
    """

    def __init__(
        self,
        *,
        orbital_content: object,
        party_body_id: str | None,
        t_hours: float = 0.0,
        plotted_course: PlottedCourse | None = None,
        orbital_scope: Scope | None = None,
    ) -> None:
        self.orbital_content = orbital_content
        self.party_body_id = party_body_id
        self.clock = Clock(t_hours=t_hours)
        self.plotted_course = plotted_course
        # read + write; defaults to system root like Session does
        self.orbital_scope = orbital_scope or Scope.system_root()
        # Any access to _snapshot is a failure (the reach-through must be gone).
        self._snapshot = _SnapshotTripwire()


@pytest.fixture
def content():
    return load_orbital_content(FIXTURES / "world_minimal")


@pytest.fixture
def standin(content):
    return NarrowOrbitalSession(orbital_content=content, party_body_id="turning_hub")


# ---------------------------------------------------------------------------
# A. The Protocol exists, is runtime-checkable, and is what the signature uses
# ---------------------------------------------------------------------------


def test_orbital_intent_session_protocol_is_runtime_checkable() -> None:
    from sidequest.orbital.intent import OrbitalIntentSession

    assert getattr(OrbitalIntentSession, "_is_protocol", False), (
        "OrbitalIntentSession must be a typing.Protocol declared in "
        "sidequest.orbital.intent (the narrow read surface)."
    )
    # @runtime_checkable lets us assert structural conformance below.
    assert getattr(OrbitalIntentSession, "_is_runtime_protocol", False), (
        "OrbitalIntentSession must be decorated @runtime_checkable."
    )


def test_handle_signature_narrowed_off_session() -> None:
    """The ``session`` parameter must be annotated with the narrow Protocol,
    not the concrete server ``Session``."""
    annotation = inspect.signature(handle_orbital_intent).parameters["session"].annotation
    # Under ``from __future__ import annotations`` this is the source string.
    assert annotation == "OrbitalIntentSession", (
        f"handle_orbital_intent's session param is annotated {annotation!r}; "
        "it must be narrowed to 'OrbitalIntentSession' (NOT 'Session')."
    )
    assert annotation != "Session"


# ---------------------------------------------------------------------------
# B. The function runs against a minimal non-Session stand-in
# ---------------------------------------------------------------------------


def test_view_map_on_standin(standin) -> None:
    resp = handle_orbital_intent(standin, _view_map("system_root"))
    assert isinstance(resp, OrbitalIntentResponse)
    assert resp.scope_center == "coyote"
    assert resp.party_at == "turning_hub"
    assert "<svg" in resp.svg or resp.svg.startswith("<?xml")
    assert not isinstance(standin, Session)  # proves the narrowing is real


def test_drill_in_on_standin_persists_scope(standin) -> None:
    resp = handle_orbital_intent(standin, _drill_in("red_prospect"))
    assert resp.scope_center == "red_prospect"
    # orbital_scope is a read+write member — the function must mutate the stand-in
    assert standin.orbital_scope.center_body_id == "red_prospect"


def test_drill_out_reads_then_writes_scope_on_standin(standin) -> None:
    handle_orbital_intent(standin, _drill_in("red_prospect"))
    resp = handle_orbital_intent(standin, _drill_out())
    # red_prospect's parent is coyote (the system primary). Behavior-preserving
    # (epic-122): drill_out stores the parent's id verbatim — Scope(center_body_id
    # ="coyote") — it does NOT normalize the primary to "<root>". The render still
    # centers on coyote either way. The read+write proof is that orbital_scope was
    # read (to find the parent) and written back on the stand-in.
    assert resp.scope_center == "coyote"
    assert standin.orbital_scope.center_body_id == "coyote"


def test_standin_conforms_to_runtime_protocol(standin) -> None:
    from sidequest.orbital.intent import OrbitalIntentSession

    assert isinstance(standin, OrbitalIntentSession)


# ---------------------------------------------------------------------------
# C. The private _snapshot reach-through is gone (tripwire + overlay path)
# ---------------------------------------------------------------------------


def test_no_plotted_course_does_not_touch_snapshot(standin) -> None:
    """With plotted_course=None the overlay is skipped — and crucially the
    function must learn that from ``session.plotted_course``, never from the
    tripwire ``session._snapshot``."""
    resp = handle_orbital_intent(standin, _view_map("system_root"))
    assert resp.next_conjunction is None or resp.next_conjunction is not None  # smoke
    # The real assertion: the call completed without tripping _SnapshotTripwire.
    assert isinstance(resp, OrbitalIntentResponse)


def test_plotted_course_read_from_narrow_surface(content) -> None:
    """When a course is set on the narrow ``plotted_course`` member, the overlay
    path must fire — proving the function reads the course from the Protocol
    surface, not from ``_snapshot``."""
    course = PlottedCourse(
        to_body_id="red_prospect",
        label="Red Prospect",
        eta_hours=80.0,
        delta_v=2.4,
        plotted_at_t_hours=0.0,
        source=CourseSource.IN_SCOPE,
    )
    standin = NarrowOrbitalSession(
        orbital_content=content,
        party_body_id="turning_hub",
        plotted_course=course,
    )
    resp = handle_orbital_intent(standin, _view_map("system_root"))
    # render_course_overlay injects a course path/chip into the SVG.
    assert "course" in resp.svg.lower(), (
        "course overlay did not render — the function did not read "
        "plotted_course from the narrow Protocol surface."
    )


def test_standin_without_orbital_content_raises(content) -> None:
    bare = NarrowOrbitalSession(orbital_content=None, party_body_id=None)
    with pytest.raises(OrbitalContentUnavailableError):
        handle_orbital_intent(bare, _view_map("system_root"))


# ---------------------------------------------------------------------------
# D. A real Session still satisfies the Protocol and drives the function
# ---------------------------------------------------------------------------


def test_real_session_satisfies_protocol_and_works() -> None:
    from sidequest.orbital.intent import OrbitalIntentSession

    snapshot = GameSnapshot(party_body_id="turning_hub")
    session = Session(snapshot, orbital_content=load_orbital_content(FIXTURES / "world_minimal"))
    # The production caller passes a real Session — it must still conform.
    assert isinstance(session, OrbitalIntentSession), (
        "real Session no longer satisfies OrbitalIntentSession — the production "
        "caller handlers/orbital_intent.py would break. Session likely needs a "
        "plotted_course accessor added during the narrowing."
    )
    resp = handle_orbital_intent(session, _drill_in("red_prospect"))
    assert resp.scope_center == "red_prospect"
    assert session.orbital_scope.center_body_id == "red_prospect"


# ---------------------------------------------------------------------------
# E. Import-direction wiring (ADR-147 law) — AST scan, not source grep
# ---------------------------------------------------------------------------


def _import_targets(tree: ast.AST) -> list[str]:
    """Every dotted module path imported, module-level OR nested/lazy."""
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            targets.append(mod)
            for alias in node.names:
                targets.append(f"{mod}.{alias.name}" if mod else alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
    return targets


def _server_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(
        {
            t
            for t in _import_targets(tree)
            if t == "sidequest.server" or t.startswith("sidequest.server.")
        }
    )


def test_intent_module_no_longer_imports_server() -> None:
    intent_py = SIDEQUEST_PKG / "orbital" / "intent.py"
    assert intent_py.is_file()
    hits = _server_imports(intent_py)
    assert hits == [], (
        "sidequest/orbital/intent.py still imports sidequest.server "
        f"({hits}); 122-4 narrows the parameter to an in-tier Protocol so this "
        "upward edge is removed (ADR-147 layering law)."
    )


def test_orbital_tier_is_server_pure() -> None:
    """intent.py was the orbital tier's last server importer — once narrowed,
    the whole tier obeys the import-direction law."""
    orbital_dir = SIDEQUEST_PKG / "orbital"
    offenders: dict[str, list[str]] = {}
    for path in sorted(orbital_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        hits = _server_imports(path)
        if hits:
            offenders[str(path.relative_to(SIDEQUEST_PKG))] = hits
    assert offenders == {}, (
        "orbital tier must not import sidequest.server. Remaining upward edges:\n"
        + "\n".join(f"  {f}: {h}" for f, h in sorted(offenders.items()))
    )
