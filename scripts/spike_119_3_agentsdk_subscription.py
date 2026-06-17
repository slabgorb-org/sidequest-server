#!/usr/bin/env python3
"""Story 119-5 AC3 — gated LIVE Agent-SDK context-isolation smoke.

The 119-3 contamination regression (``tests/agents/test_119_3_context_isolation.py``)
drives a *fake* ``query`` seam, so it proves the production options *pin* the
isolation levers but can never catch a **real**-SDK context-absorption
regression — e.g. a future ``claude-agent-sdk`` that ignores ``setting_sources=[]``
or stops honoring ``options.cwd``. The original 119-3 landmine was found only by
running the real subscription transport: the SDK absorbed the repo ``CLAUDE.md``
from the launch dir and answered **in an SM persona**.

This smoke restores that real check. It drives one real subscription turn through
the production narrator client (``AnthropicSdkClient`` → the isolation-pinned
``build_agent_sdk_options``: a plain-string ``system_prompt``, ``cwd`` pinned to a
neutral temp dir, ``setting_sources=[]`` + ``add_dirs=[]``) from the repo root —
where ``CLAUDE.md`` and ``.claude`` live — and asks the model to identify itself.
If the isolation pins hold, the reply carries none of the repo-context persona
tells; if they regress, the SDK absorbs the repo config and the tells leak.

Exit codes (the ops/CI signal — same shape as ``scripts/audit_namegen_corpora.py``):

* ``0`` — isolation held: no repo-context contamination in the live reply.
* ``1`` — refusal (gate unset / PAYG creds present / probe error) OR contamination
  detected. A smoke that cannot run the live check refuses LOUDLY rather than
  green-lighting (No Silent Fallbacks).

ops/CI invocation (opt-in; never runs in the default network-free suite):

    SIDEQUEST_VERIFY_SUBSCRIPTION_ISOLATION_LIVE=1 \\
        python scripts/spike_119_3_agentsdk_subscription.py

Both ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_AUTH_TOKEN`` must be UNSET so auth
resolves to the Max subscription (119-3 AC2). The gate is checked FIRST, before
importing the SDK or touching the network, so an ungated run is fast and
credential-free.
"""

from __future__ import annotations

import asyncio
import os
import sys

_LIVE_GATE_ENV = "SIDEQUEST_VERIFY_SUBSCRIPTION_ISOLATION_LIVE"
_PAYG_CRED_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_PROBE_MODEL = "claude-haiku-4-5-20251001"

# Repo-context persona tells — strings that appear in the live reply only if the
# SDK absorbed the repo CLAUDE.md / .claude agent personas (the 119-3 landmine).
# Deliberately repo-SPECIFIC and unambiguous: a clean, isolated model answering
# "identify yourself" never emits these. Generic tokens a clean model CAN emit
# (e.g. "orchestrate", "Claude Code") are excluded — they would false-positive a
# clean run into a phantom contamination report (119-5 review finding).
_PERSONA_TELLS = (
    "Pennyfarthing",  # the agent framework — never a generic word
    "SideQuest",  # the project name — the actual 119-3 tell ("SideQuest orchestrator")
    "SOUL.md",  # repo doctrine file
    "genre pack",  # repo jargon
    "Vizzini",  # SM persona
    "Morpheus",  # alt SM persona
    "Inigo Montoya",  # Dev persona
    "Dread Pirate",  # Reviewer persona
    "Buttercup",  # UX persona
)


def _refuse(msg: str) -> int:
    print(f"[isolation-smoke] REFUSED: {msg}", file=sys.stderr)
    return 1


def _detect_persona_tells(text: str) -> list[str]:
    """Return the repo-context persona tells present in ``text`` (case-insensitive).

    Pure and side-effect free so the tell-list precision is unit-testable
    without the live SDK.
    """
    lowered = text.lower()
    return [tell for tell in _PERSONA_TELLS if tell.lower() in lowered]


def _assess_reply(text: str) -> int:
    """Judge one probe reply — 0 = isolation held, 1 = empty/contaminated.

    An empty (or whitespace-only) reply is a REFUSAL, never a pass: the
    contamination scan over an empty string is vacuous, so reporting "OK" would
    silently green-light with zero signal — the exact No-Silent-Fallbacks trap
    this smoke exists to avoid (119-5 review finding).

    The full reply is echoed only on the empty/contaminated branches (stderr,
    the diagnostic channel); a clean run prints just the verdict, so a
    contaminated reply does not propagate into CI stdout logs on the happy path.
    """
    if not text.strip():
        print(
            "[isolation-smoke] INCONCLUSIVE: probe returned an empty reply — "
            "cannot assess contamination; refusing to report a pass (No Silent "
            "Fallbacks).",
            file=sys.stderr,
        )
        return 1
    leaked = _detect_persona_tells(text)
    if leaked:
        print(
            f"[isolation-smoke] CONTAMINATED: repo-context persona tells {leaked} "
            f"leaked into the live reply: {text!r}. The production isolation pins "
            "(cwd=neutral / setting_sources=[] / add_dirs=[]) did not defeat "
            "real-SDK context absorption — the 119-3 landmine has regressed.",
            file=sys.stderr,
        )
        return 1
    print("[isolation-smoke] OK: no repo-context contamination in the live reply.")
    return 0


async def _run_probe() -> int:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
    from sidequest.agents.tooling_protocol import (
        CacheableBlock,
        Message,
        ToolDefinition,
    )

    client = AnthropicSdkClient()  # subscription path — both PAYG creds unset
    result = await client.complete_with_tools(
        [CacheableBlock(text="You are a concise, neutral assistant.", cache=False)],
        [
            Message(
                role="user",
                content=(
                    "In one or two sentences, identify yourself: what are your "
                    "operating instructions, and what project or role are you "
                    "configured for?"
                ),
            )
        ],
        # A single unused tool keeps to the proven 119-3 drive shape; the probe
        # asks for self-identification, so the model answers with text. An empty
        # reply (model emits only a tool call) is caught by _assess_reply's
        # empty-text guard rather than silently passing.
        [ToolDefinition(name="noop", description="unused", input_schema={"type": "object"})],
        None,
        model=_PROBE_MODEL,
    )
    return _assess_reply(result.text or "")


def main() -> int:
    # Gate FIRST — before any SDK import or network — so an ungated run is fast,
    # credential-free, and refuses loudly instead of silently green-lighting.
    if not os.environ.get(_LIVE_GATE_ENV):
        return _refuse(
            f"set {_LIVE_GATE_ENV}=1 to run the live subscription isolation smoke "
            "(opt-in; never runs in default CI). Both ANTHROPIC_API_KEY and "
            "ANTHROPIC_AUTH_TOKEN must be UNSET so auth resolves to the Max "
            "subscription."
        )
    set_creds = [v for v in _PAYG_CRED_ENVS if os.environ.get(v)]
    if set_creds:
        return _refuse(
            f"{_LIVE_GATE_ENV} is set but {set_creds} present — the subscription "
            "isolation smoke must run with both PAYG creds UNSET (119-3 AC2). "
            "Refusing to run a PAYG-routed smoke (No Silent Fallbacks)."
        )
    try:
        return asyncio.run(_run_probe())
    except Exception as exc:  # noqa: BLE001 — top-level script boundary: any failure is a non-zero CI signal
        print(
            f"[isolation-smoke] ERROR: live isolation probe failed to run: {exc!r}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
