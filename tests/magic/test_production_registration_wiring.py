"""ADR-126 production-path wiring: MAGIC_PLUGINS must populate via prod import.

Story 81-1. The bug: nothing in *production* imports ``sidequest.magic.plugins``
(the package whose star-imports populate ``MAGIC_PLUGINS`` as an import
side-effect). Only tests import it — directly (``tests/magic/test_wiring.py``)
or via the session-scoped autouse fixture in ``tests/magic/conftest.py``. So at
runtime ``MAGIC_PLUGINS`` is ``{}`` and every plugin-declared working falls into
the ``plugin_known_but_not_registered`` DEEP_RED branch of ``validator.validate``.

Why these tests run in a SUBPROCESS, not in-process:
``tests/magic/conftest.py`` has a ``scope="session", autouse=True`` fixture that
imports ``sidequest.magic.plugins`` for the whole magic test session. That import
populates the process-global registry, so ANY in-process assertion about the
production path is masked — it would pass even against the broken code. A fresh
interpreter that imports ONLY the production entrypoint is the sole way to prove
the registry is populated *by production wiring* and not by test scaffolding.

This is exactly the false-green that ``test_wiring.py`` suffers from:
``test_plugin_registry_has_innate_and_item_legacy`` imports the plugins package
itself, so it stays green while production stays empty (see story 81-1).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# All three v1 plugins that the production path MUST register.
_EXPECTED_PLUGIN_IDS = {"innate_v1", "item_legacy_v1", "learned_v1"}


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh interpreter (no pytest, no conftest fixtures).

    Returns the completed process. The script is responsible for raising
    (non-zero exit) on failure; the caller asserts on ``returncode`` and
    surfaces captured stdout+stderr for debugging.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _fail_message(label: str, proc: subprocess.CompletedProcess[str]) -> str:
    return (
        f"{label} (subprocess exit={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )


def test_production_import_alone_populates_registry():
    """AC#1/AC#4: ``import sidequest.magic`` (NOT the plugins package) populates
    MAGIC_PLUGINS with the full v1 set.

    RED on current develop: the production entrypoint leaves the registry empty.
    The subprocess imports ONLY ``sidequest.magic`` and must NEVER reference
    ``sidequest.magic.plugins`` — doing so would prove nothing (that is the bug).
    """
    proc = _run_isolated(
        f"""
        import sidequest.magic  # production entrypoint ONLY
        from sidequest.magic.plugin import MAGIC_PLUGINS

        registered = set(MAGIC_PLUGINS)
        expected = {sorted(_EXPECTED_PLUGIN_IDS)!r}
        missing = set(expected) - registered
        assert not missing, (
            f"production import sidequest.magic did not register {{sorted(missing)}}; "
            f"registry={{sorted(registered)}}"
        )
        print("OK", sorted(registered))
        """
    )
    assert proc.returncode == 0, _fail_message(
        "MAGIC_PLUGINS empty via the production import path", proc
    )


def test_validator_does_not_misfire_for_registered_plugin_via_production_path():
    """AC#2 (positive): through the production import path, a correctly-configured
    working for a registered plugin must NOT receive ``plugin_known_but_not_registered``.

    This is the real-world consequence of the bug: with the registry empty,
    ``validator.validate`` mis-flags valid content as a config failure and never
    runs plugin-side validation. RED on current develop. Subprocess-isolated so
    the conftest autouse fixture cannot mask the empty registry.
    """
    proc = _run_isolated(
        """
        import sidequest.magic  # production entrypoint ONLY
        from sidequest.magic.validator import validate
        from sidequest.magic.models import (
            MagicWorking, WorldMagicConfig, WorldKnowledge,
        )

        config = WorldMagicConfig(
            world_slug="t", genre_slug="g",
            allowed_sources=["innate"], active_plugins=["innate_v1"],
            intensity=0.5, world_knowledge=WorldKnowledge(primary="folkloric"),
            visibility={"primary": "feared"}, ledger_bars=[], hard_limits=[],
            cost_types=[], narrator_register="standard",
        )
        working = MagicWorking(
            plugin="innate_v1", mechanism="native", actor="rux",
            domain="psychic", narrator_basis="a quiet act of will",
        )
        reasons = [f.reason for f in validate(working, config)]
        assert "plugin_known_but_not_registered" not in reasons, (
            f"registered plugin mis-flagged as not-registered; reasons={reasons}"
        )
        print("OK", reasons)
        """
    )
    assert proc.returncode == 0, _fail_message(
        "validator mis-fired plugin_known_but_not_registered for a registered "
        "plugin via the production path",
        proc,
    )


def test_validator_still_flags_genuinely_unregistered_plugin(world_config):
    """AC#2 (negative guard): a plugin id that is KNOWN to ``_PLUGIN_SOURCE`` but
    has no implemented module must STILL hit ``plugin_known_but_not_registered``.

    ``divine_v1`` is listed in ``validator._PLUGIN_SOURCE`` (forward-looking) but
    ships no plugin module, so it is never registered — even after the fix. This
    guard ensures the fix does not "solve" the bug by deleting the branch. Runs
    in-process: it depends on an inherently-unregistered id, not on production
    wiring, so the conftest fixture does not affect the outcome.
    """
    from sidequest.magic.models import MagicWorking
    from sidequest.magic.validator import _PLUGIN_SOURCE, validate

    assert "divine_v1" in _PLUGIN_SOURCE, (
        "test premise broke: divine_v1 must be a known-but-unimplemented plugin id"
    )

    config = world_config.model_copy(
        update={
            "active_plugins": ["divine_v1"],
            "allowed_sources": ["divine"],
        }
    )
    working = MagicWorking(
        plugin="divine_v1",
        mechanism="granted",
        actor="rux",
        domain="divinatory",
        narrator_basis="a borrowed blessing",
    )
    reasons = [f.reason for f in validate(working, config)]
    assert "plugin_known_but_not_registered" in reasons, (
        f"known-but-unregistered plugin should still be flagged; reasons={reasons}"
    )


def test_production_validation_module_imports_without_cycle():
    """AC#3 + AC#1: importing the production call site (``narration_apply``,
    which reaches the validator) must succeed AND leave MAGIC_PLUGINS populated.

    Two assertions in one prod-path probe:
    - No ImportError / circular import (the *guard* half). The fix wires the
      plugins package into ``sidequest.magic.__init__`` whose submodules import
      back from ``sidequest.magic.plugin``/``models``; if that introduces a cycle
      this import raises here and fails loud — exactly what AC#3 requires.
    - The registry is non-empty after reaching the call site (RED now). On
      current develop ``narration_apply`` imports the bare registry only, so
      MAGIC_PLUGINS stays ``{}`` — this fails today and passes after the fix.
    """
    proc = _run_isolated(
        """
        import sidequest.server.narration_apply  # production magic_validate call site
        from sidequest.magic.plugin import MAGIC_PLUGINS

        # Reaching the call site must also have populated the registry: the
        # production validation path is only correct if the plugins are live.
        assert MAGIC_PLUGINS, (
            "narration_apply imported but MAGIC_PLUGINS is still empty — the "
            "production validation path cannot validate plugin workings"
        )
        print("OK", sorted(MAGIC_PLUGINS))
        """
    )
    assert proc.returncode == 0, _fail_message(
        "production validation module failed to import (cycle?) or left registry empty",
        proc,
    )
