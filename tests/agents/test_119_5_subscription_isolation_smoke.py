"""Story 119-5 RED — AC3: a gated LIVE Agent-SDK context-isolation smoke.

The 119-3 contamination regression (``test_119_3_context_isolation.py``) drives
a **fake** ``query`` seam: ``ContaminatingFakeQuery`` only leaks the repo persona
when ``options_pin_isolation(opts)`` is false. It proves the options builder
*pins* the levers, but it can never catch a **real**-SDK context-absorption
regression — e.g. a future ``claude-agent-sdk`` that ignores ``setting_sources=[]``,
or a neutral ``cwd`` that stops being neutral. The original 119-3 spike found the
landmine (the SDK answered in an SM persona) only by running the *real*
subscription transport.

AC3 restores that real check as a gated smoke: ``scripts/spike_119_3_agentsdk_subscription.py``,
runnable on demand in ops/CI, that drives the production isolation options
against the live subscription and exits non-zero if the model output is
contaminated by repo context.

Contract pinned here (No Silent Fallbacks — a smoke that cannot run live must
refuse, never green-light):

* the spike script exists as a deliverable artifact;
* run WITHOUT the live gate it refuses loudly (non-zero, names the gate env);
* the live run is opt-in behind ``SIDEQUEST_VERIFY_SUBSCRIPTION_ISOLATION_LIVE``
  and exits 0 only when isolation holds.

Script is exercised by subprocess (the ``scripts/`` test precedent —
``tests/scripts/test_audit_namegen_corpora.py``), not import, so packaging and
the real CLI entrypoint are covered as CI runs it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

# tests/agents/<this> -> parents[2] == server repo root.
_SERVER_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPIKE_SCRIPT = _SERVER_REPO_ROOT / "scripts" / "spike_119_3_agentsdk_subscription.py"

# The opt-in gate the live smoke is fenced behind (mirrors the 91-3
# SIDEQUEST_VERIFY_HAIKU_CACHE_LIVE pattern — never runs in default CI).
_LIVE_GATE_ENV = "SIDEQUEST_VERIFY_SUBSCRIPTION_ISOLATION_LIVE"
_PAYG_CRED_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _env_without_gate() -> dict[str, str]:
    env = dict(os.environ)
    env.pop(_LIVE_GATE_ENV, None)
    return env


def test_spike_script_exists() -> None:
    """The gated live smoke must exist as a real deliverable (AC3).

    RED today: ``scripts/spike_119_3_agentsdk_subscription.py`` does not exist —
    119-3 left no spike script in ``scripts/``.
    """
    assert _SPIKE_SCRIPT.is_file(), (
        f"AC3 requires the gated live isolation smoke at {_SPIKE_SCRIPT} — it "
        "does not exist yet (no spike script survived 119-3)"
    )


def test_spike_script_refuses_without_live_gate() -> None:
    """Run without the gate the smoke must REFUSE loudly, not silently pass.

    A live smoke that quietly exits 0 when it did not actually run the
    subscription check is a silent fallback — it would green-light CI while
    proving nothing. Without ``SIDEQUEST_VERIFY_SUBSCRIPTION_ISOLATION_LIVE`` the
    script must exit non-zero and name the gate env so the operator knows how to
    run it. The gate check must come first — before any network / SDK import —
    so this assertion needs no credentials.
    """
    if not _SPIKE_SCRIPT.is_file():
        pytest.fail(f"{_SPIKE_SCRIPT} does not exist — cannot verify its gate refusal (AC3)")

    proc = subprocess.run(
        [sys.executable, str(_SPIKE_SCRIPT)],
        cwd=str(_SERVER_REPO_ROOT),
        env=_env_without_gate(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0, (
        "ungated run must refuse with a non-zero exit — a smoke that exits 0 "
        f"without running the live check is a silent fallback. stdout={proc.stdout!r}"
    )
    combined = proc.stdout + proc.stderr
    assert _LIVE_GATE_ENV in combined, (
        "the ungated refusal must name the gate env so the operator knows how "
        f"to run it; got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


@pytest.mark.timeout(180)
def test_subscription_isolation_live() -> None:
    """THE real-SDK proof: the production isolation options keep the live
    subscription from absorbing repo context.

    Opt-in like the composer's Gymnopedie smoke: skips unless the gate is set;
    fails loud (not skip) if the gate is set while a PAYG credential is present
    (the subscription path requires both creds UNSET — 119-3 AC2). Exit 0 from
    the script means isolation held; non-zero means contamination was detected.
    """
    if not os.environ.get(_LIVE_GATE_ENV):
        pytest.skip(f"set {_LIVE_GATE_ENV}=1 (subscription auth, both PAYG creds unset) to run")

    set_creds = [v for v in _PAYG_CRED_ENVS if os.environ.get(v)]
    if set_creds:
        pytest.fail(
            f"{_LIVE_GATE_ENV} set but {set_creds} present — the subscription "
            "isolation smoke must run with ANTHROPIC_API_KEY and "
            "ANTHROPIC_AUTH_TOKEN both UNSET (119-3 AC2). Refusing to run a "
            "PAYG-routed smoke (No Silent Fallbacks)."
        )
    if not _SPIKE_SCRIPT.is_file():
        pytest.fail(f"{_SPIKE_SCRIPT} does not exist — cannot run the live smoke (AC3)")

    proc = subprocess.run(
        [sys.executable, str(_SPIKE_SCRIPT)],
        cwd=str(_SERVER_REPO_ROOT),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=170,
    )
    assert proc.returncode == 0, (
        "live isolation smoke detected repo-context contamination (or failed to "
        f"run): rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


# ---------------------------------------------------------------------------
# AC3 — detection logic (pure helpers, no live SDK). 119-5 review rework:
# the empty-text guard and tell-list precision are unit-testable without a
# subscription call, so they are pinned here directly.
# ---------------------------------------------------------------------------


def _load_spike_module() -> ModuleType:
    """Import the spike script by path (scripts/ is not an importable package).

    Loading runs only module-level defs/constants — ``main()`` is under
    ``if __name__ == '__main__'`` and the SDK imports live inside ``_run_probe``,
    so import is cheap and credential-free.
    """
    if not _SPIKE_SCRIPT.is_file():
        pytest.fail(f"{_SPIKE_SCRIPT} does not exist — cannot import the spike helpers (AC3)")
    spec = importlib.util.spec_from_file_location(
        "spike_119_3_agentsdk_subscription", _SPIKE_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assess_reply_empty_is_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty / whitespace-only reply must REFUSE (return 1), not pass.

    The contamination scan over an empty string is vacuous — reporting "OK"
    would silently green-light with zero signal (No Silent Fallbacks). This is
    the 119-5 review HIGH finding.
    """
    spike = _load_spike_module()

    assert spike._assess_reply("") == 1, "empty reply must be a refusal, not a pass"
    assert spike._assess_reply("   \n\t ") == 1, "whitespace-only reply must be a refusal"

    err = capsys.readouterr().err
    assert "INCONCLUSIVE" in err, "the empty-reply refusal must announce itself loudly on stderr"


def test_assess_reply_clean_reply_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """A clean self-identification — including generic words a clean model emits
    (orchestrate / code) — must PASS (return 0). Guards against the tell-list
    false-positive flagged in 119-5 review (MEDIUM)."""
    spike = _load_spike_module()

    clean = "I am Claude, an AI assistant made by Anthropic. I can help orchestrate your tasks and write code."
    assert spike._assess_reply(clean) == 0, (
        "a clean reply with generic words must not be flagged as contamination"
    )
    out = capsys.readouterr().out
    assert "OK" in out
    # The full reply must NOT be dumped to stdout on the clean path (CI-log leak guard).
    assert clean not in out, "the clean path must not echo the full reply to stdout"


def test_assess_reply_contaminated_reply_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A reply carrying a repo-specific persona tell must FAIL (return 1)."""
    spike = _load_spike_module()

    contaminated = (
        "As Vizzini, the SideQuest orchestrator, my operating instructions come from SOUL.md."
    )
    assert spike._assess_reply(contaminated) == 1, (
        "a repo-tell reply must be flagged as contamination"
    )
    err = capsys.readouterr().err
    assert "CONTAMINATED" in err


def test_detect_persona_tells_ignores_generic_tokens() -> None:
    """Generic tokens a clean model can emit must NOT be tells (119-5 MEDIUM).

    `orchestrator` / `Claude Code` were removed from the list precisely because
    a clean, isolated model emits them in benign self-description.
    """
    spike = _load_spike_module()

    benign = "I'm Claude Code and I can orchestrate your build pipeline."
    assert spike._detect_persona_tells(benign) == [], (
        f"generic tokens must not be flagged; got {spike._detect_persona_tells(benign)!r}"
    )


def test_detect_persona_tells_flags_repo_markers() -> None:
    """Unambiguous repo markers (project name, doctrine file, persona names)
    must be detected (case-insensitive)."""
    spike = _load_spike_module()

    text = "pennyfarthing runs SideQuest; see soul.md. As Morpheus, the Scrum Master..."
    found = spike._detect_persona_tells(text)
    assert "Pennyfarthing" in found
    assert "SideQuest" in found
    assert "SOUL.md" in found
    assert "Morpheus" in found
