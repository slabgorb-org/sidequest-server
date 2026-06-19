"""Regression guard: the native beat/dial ConfrontationOverlay must NOT be projected
for a Fate pack (ADR-144 / playtest 150-2 BLOCKING).

A Fate conflict's player surface is FATE_STATE (``build_fate_state_payload`` /
``_maybe_emit_fate_state``). The native ``build_confrontation_payload`` is the
d20/beat builder; for a Fate confrontation it reaches ``FateRulesetModule.compute_dc``,
which raises ``NotImplementedError`` (the ADR-144 No-Silent-Fallbacks guard — correct,
but it bricks the turn). Before this fix, ``_execute_narration_turn`` (per turn) and
``ConnectHandler.handle`` (slug-resume) called the native builder unconditionally, so
every Fate standoff bricked the moment it seated. The fix is the
``should_emit_native_confrontation`` gate at both call sites.

Two guard kinds (mirrors ``test_fate_state_emit_wiring.py``):

  1. Unit — the gate excludes Fate and admits every non-Fate ruleset (and ``None``).
  2. Wiring (reflection on the compiled code, NOT a source-text grep — CLAUDE.md):
     both production paths reference the gate, so the native projection is actually
     gated, not merely gate-able.
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.genre.models.rules import RulesConfig
from sidequest.server.dispatch.confrontation import should_emit_native_confrontation

# ---------------------------------------------------------------------------
# 1 — Unit: the gate decision.
# ---------------------------------------------------------------------------


def test_gate_excludes_fate() -> None:
    # Duck-typed (the gate reads only ``.ruleset``) — a real RulesConfig(ruleset="fate")
    # requires a full FateConfig block; the field-name anchor below uses the real type.
    assert should_emit_native_confrontation(SimpleNamespace(ruleset="fate")) is False


def test_gate_admits_non_fate_rulesets() -> None:
    for rs in ("dial", "swn", "wwn", "cwn", "awn"):
        assert should_emit_native_confrontation(SimpleNamespace(ruleset=rs)) is True, (
            f"native confrontation must still project under the {rs!r} ruleset"
        )


def test_gate_admits_none_rules() -> None:
    # Legacy/bootstrap callers with no pack in hand keep the prior native behavior.
    assert should_emit_native_confrontation(None) is True


def test_gate_reads_the_real_ruleset_field() -> None:
    # Anchor the field name against the real type: a bare ``dial`` RulesConfig is the
    # one ruleset that constructs without a per-ruleset config block, so it pins
    # ``rules.ruleset`` without dragging in a FateConfig/SwnConfig fixture.
    assert should_emit_native_confrontation(RulesConfig(ruleset="dial")) is True


# ---------------------------------------------------------------------------
# 2 — Wiring: both native-confrontation emit paths consult the gate.
# ---------------------------------------------------------------------------


def _referenced_names(code: object) -> set[str]:
    """All names referenced by a compiled code object, recursing nested code objects
    (the reflection-based wiring check allowed by CLAUDE.md — NOT a source-text grep)."""
    names: set[str] = set(getattr(code, "co_names", ()))
    for const in getattr(code, "co_consts", ()):
        if hasattr(const, "co_names"):
            names |= _referenced_names(const)
    return names


def test_gate_is_wired_into_narration_turn() -> None:
    """Site A: the per-turn NARRATION_END emit path gates the native projection."""
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

    referenced = _referenced_names(WebSocketSessionHandler._execute_narration_turn.__code__)
    assert "should_emit_native_confrontation" in referenced, (
        "should_emit_native_confrontation is never referenced by _execute_narration_turn — "
        "the per-turn native confrontation projection is not gated off for Fate"
    )
    # Sanity anchor: the native builder is still referenced from this method (the thing
    # we gate). If this fails, the introspection target moved, not the gate wiring.
    assert "build_confrontation_payload" in referenced


def test_gate_is_wired_into_slug_resume() -> None:
    """Site B: the slug-resume confrontation bootstrap gates the native projection."""
    from sidequest.handlers.connect import ConnectHandler

    referenced = _referenced_names(ConnectHandler.handle.__code__)
    assert "should_emit_native_confrontation" in referenced, (
        "should_emit_native_confrontation is never referenced by ConnectHandler.handle — "
        "the slug-resume confrontation bootstrap is not gated off for Fate"
    )
    # Sanity anchor: the resume bootstrap still references the native frame supplier.
    assert "make_confrontation_frame_supplier" in referenced
