"""Story 119-5 RED — AC2: ``_neutral_cwd()`` must not leak a temp dir per process.

Story 119-3 pinned Agent-SDK context isolation by pointing ``cwd`` at a
process-stable empty temp dir (``tempfile.mkdtemp(prefix="sidequest-agentsdk-cwd-")``)
so the SDK absorbs no repo ``CLAUDE.md`` / ``.claude``. That dir is created
lazily and **never removed** — one empty ``sidequest-agentsdk-cwd-*`` dir is
left behind in ``$TMPDIR`` for every process that builds any Agent-SDK options
(narrator, intent router, classifiers).

AC2: register an ``atexit`` cleanup so the neutral cwd is removed when the
process exits. The contract these tests pin:

* creating the neutral cwd registers a cleanup callback via ``atexit.register``;
* invoking that callback deletes the temp dir.

Behavioral, not a source grep (CLAUDE.md "No Source-Text Wiring Tests"): we spy
on ``atexit.register`` and then *run* the captured callback to prove it removes
the real directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


def test_neutral_cwd_registers_atexit_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating the neutral cwd must register an ``atexit`` cleanup.

    RED today: ``_neutral_cwd()`` mkdtemp's the dir and registers nothing, so
    ``captured`` stays empty and the dir leaks for the life of the process.
    """
    import atexit

    from sidequest.agents import anthropic_sdk_client as sdk

    captured: list = []

    def _spy_register(fn, *args, **kwargs):  # noqa: ANN001, ANN202
        captured.append((fn, args, kwargs))
        return fn

    monkeypatch.setattr(atexit, "register", _spy_register)
    # Force a fresh creation so registration (which happens at creation) fires.
    monkeypatch.setattr(sdk, "_AGENT_SDK_CWD", None, raising=False)

    path = sdk._neutral_cwd()
    try:
        assert Path(path).is_dir(), "the neutral cwd must be a real directory"
        assert captured, (
            "creating the neutral cwd must register an atexit cleanup "
            "(AC2) — today it registers nothing and the temp dir leaks "
            "one-per-process"
        )

        # The registered callback must actually remove the directory.
        for fn, args, kwargs in captured:
            fn(*args, **kwargs)
        assert not Path(path).exists(), (
            "the registered atexit cleanup must delete the neutral cwd; "
            f"{path!r} still exists after invoking it"
        )
    finally:
        shutil.rmtree(path, ignore_errors=True)
        # Reset the module global so later tests/process get a fresh dir.
        monkeypatch.setattr(sdk, "_AGENT_SDK_CWD", None, raising=False)


def test_neutral_cwd_is_idempotent_within_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cleanup must not break the process-stable contract: repeated
    ``_neutral_cwd()`` calls within a live process return the SAME dir (the
    isolation pin is process-stable), so exactly one dir is registered for
    cleanup — not a fresh leak per call."""
    import atexit

    from sidequest.agents import anthropic_sdk_client as sdk

    register_count = 0

    def _spy_register(fn, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal register_count
        register_count += 1
        return fn

    monkeypatch.setattr(atexit, "register", _spy_register)
    monkeypatch.setattr(sdk, "_AGENT_SDK_CWD", None, raising=False)

    first = sdk._neutral_cwd()
    try:
        second = sdk._neutral_cwd()
        assert first == second, (
            "the neutral cwd must stay process-stable across calls (isolation "
            "pin) — a fresh dir per call would re-leak and defeat the lazy cache"
        )
        assert register_count == 1, (
            "exactly one atexit cleanup must be registered for the single "
            f"process-stable dir; got {register_count} registrations"
        )
    finally:
        shutil.rmtree(first, ignore_errors=True)
        monkeypatch.setattr(sdk, "_AGENT_SDK_CWD", None, raising=False)
