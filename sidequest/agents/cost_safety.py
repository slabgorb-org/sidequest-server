"""Cross-call-site cost safety state — ADR-134 extended to all models (91-4).

Story 91-4 (epic 91 "Dark Spend"): ADR-134's two billing-safety layers —
the multi-trigger cost-runaway *detector* and the per-session cumulative
hard-kill *ceiling* — originally lived as instance state inside
``AnthropicSdkClient``, which only the narrator/dungeon-curate path
constructs long-lived. The Haiku adapters (``_AsideLlm``,
``_IntentRouterLlm`` in ``llm_factory``) are constructed fresh per call,
so instance state can never accumulate there; this module holds the
PROCESS-LEVEL ledger every call site shares:

- **One cumulative pot per session.** ``SessionCostLedger.cumulative_cost_usd``
  is keyed on ``session_id`` and fed by narrator, aside, and router spend
  alike. The ``AnthropicSdkClient`` constructor aliases its
  ``_session_cumulative_cost_usd`` / ``_session_ceiling_announced`` attrs
  to these shared dicts, so the narrator's pre-existing ceiling machinery
  and per-turn ``session.cost_running_total`` pulse see the combined
  figure with no behavior change.
- **Per-(session, caller) rolling baselines for the adapters.** Haiku
  traffic must NOT train the narrator's per-session baseline (a shared
  window would drag the mean to fractions of a cent and false-fire
  ``cost_multiple`` on every healthy Sonnet turn) — the narrator keeps
  its instance windows keyed on plain ``session_id`` (61-followup-A
  contract); adapter windows live here keyed ``(session_id, caller)``.
- **One detector implementation.** ``check_and_emit_runaway`` is the
  trigger comparator + emit both the narrator client and the ledger
  delegate to, so the four ADR-134 triggers and their priority order
  cannot drift between call sites.

The exceptions (``AnthropicSdkConfigError``,
``AnthropicSdkCostCeilingExceeded``) stay defined in
``anthropic_sdk_client`` — every existing consumer imports them from
there — and are late-imported here at raise time (the same
function-level-import pattern 91-1 used to dodge the
``llm_factory ↔ anthropic_sdk_client`` cycle; ``anthropic_sdk_client``
imports this module at its top, so this module must not import it back
at module level).

Test isolation: state here is process-global by design. The suite-wide
autouse fixture (``tests/conftest.py``) calls
``ledger().reset_for_tests()`` before every test.
"""

from __future__ import annotations

import logging
import math
import os
from collections import deque
from typing import TYPE_CHECKING, Any

from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish_event

if TYPE_CHECKING:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkCostCeilingExceeded

logger = logging.getLogger(__name__)


# Story 61-4 — Cost-runaway fingerprint detector. Two parallel rolling
# baselines (K=10 each) compare each SDK call against either an observed
# baseline (post-warmup) or warmup floors (pre-warmup) for cost and input
# tokens. Either trigger fires the same ``cost_runaway_suspected`` event,
# distinguished by a ``trigger`` discriminator field. The 60K-in/12-out
# fingerprint from the 2026-05-23 incident hits both; collapse to a single
# event with ``io_fingerprint`` priority (decision C). Constants moved
# here from ``anthropic_sdk_client`` by 91-4 (re-bound there for existing
# importers); semantics unchanged.

_BASELINE_WINDOW_K: int = 10
_WARMUP_COST_USD_FLOOR: float = 0.03
_WARMUP_INPUT_TOKENS_FLOOR: int = 12_000
_COST_TRIGGER_MULTIPLE: float = 5.0
_IO_FINGERPRINT_INPUT_MULTIPLE: float = 2.0
_IO_FINGERPRINT_OUTPUT_CEILING: int = 50
# Architect spec-check A: absolute ceiling that fires REGARDLESS of the
# rolling baseline — the safety net for the trained-into-silence case.
_ABSOLUTE_COST_USD_FLOOR: float = 0.30

# Story 61-followup-D §A — clamp the rolling-mean baseline at 3× the
# warmup floors so a sustained runaway cannot raise the trip threshold
# without bound.
_BASELINE_COST_CEILING: float = 3.0 * _WARMUP_COST_USD_FLOOR
_BASELINE_INPUT_CEILING: int = 3 * _WARMUP_INPUT_TOKENS_FLOOR

# Story 61-followup-D §B — absolute input_tokens floor; catches the
# high-output sibling of the 60K-in/12-out fingerprint.
_ABSOLUTE_INPUT_TOKENS_FLOOR: int = 40_000

# Story 61-followup-D §C — per-session cumulative HARD KILL default.
_SESSION_COST_CEILING_USD: float = 10.0


def parse_session_cost_ceiling_usd() -> float:
    """Resolve the per-session cost ceiling from the environment.

    Moved from the ``AnthropicSdkClient`` constructor (91-4) so the Haiku
    adapters apply the SAME No-Silent-Fallbacks validation at THEIR
    construction: ``float('nan')`` makes every ``cumulative >= ceiling``
    comparison False (silently disabling the kill), ``inf`` makes the
    ceiling unreachable — both raise ``AnthropicSdkConfigError``, as do
    non-parseable and non-positive values.
    """
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkConfigError

    ceiling_env = os.environ.get("SIDEQUEST_SESSION_COST_CEILING_USD")
    if ceiling_env is None:
        return _SESSION_COST_CEILING_USD
    try:
        parsed = float(ceiling_env)
    except ValueError as exc:
        raise AnthropicSdkConfigError(
            f"SIDEQUEST_SESSION_COST_CEILING_USD={ceiling_env!r} could not be parsed as a float."
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise AnthropicSdkConfigError(
            f"SIDEQUEST_SESSION_COST_CEILING_USD={ceiling_env!r} must be a finite positive number."
        )
    return parsed


def build_ceiling_exceeded(
    *,
    session_id: str,
    cumulative: float,
    ceiling_usd: float,
) -> AnthropicSdkCostCeilingExceeded:
    """Construct the typed ceiling-exceeded exception with the canonical
    message + actionable fields. Centralized (61-followup-D, relocated by
    91-4) so no raise site — narrator pre-flight, adapter pre-flight, or
    crossing update — can drift in wording or field shape."""
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkCostCeilingExceeded

    return AnthropicSdkCostCeilingExceeded(
        f"Session {session_id!r} has exceeded its ${ceiling_usd:.2f} ceiling "
        f"(cumulative=${cumulative:.4f}).",
        session_id=session_id,
        cumulative_cost_usd=cumulative,
        ceiling_usd=ceiling_usd,
    )


def check_and_emit_runaway(
    *,
    cost_window: deque[float] | None,
    input_window: deque[int] | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    model: str,
    session_id: str,
    caller: str,
) -> None:
    """Evaluate the four ADR-134 triggers against PRIOR observations and
    fire ``cost_runaway_suspected`` if any matches.

    Pure comparator + emit: the caller owns window storage and appends
    AFTER this check (so the comparator never sees the call it is
    judging). Shared by the narrator client (instance windows keyed on
    ``session_id``) and the adapter ledger (shared windows keyed on
    ``(session_id, caller)``) — story 91-4's "one detector, every call
    site" consolidation. The event gains a ``caller`` field so the GM
    panel can attribute cross-model alarms (component stays
    ``narrator.sdk`` so existing GM-panel filters keep matching).
    """
    warmup = cost_window is None or input_window is None or len(cost_window) < _BASELINE_WINDOW_K
    if warmup:
        baseline_cost = _WARMUP_COST_USD_FLOOR
        baseline_input: float = _WARMUP_INPUT_TOKENS_FLOOR
    else:
        # 61-followup-D §A — clamp the rolling mean at the ceiling so the
        # comparator cannot self-train into silence under sustained ramps.
        assert cost_window is not None and input_window is not None
        observed_cost = sum(cost_window) / len(cost_window)
        observed_input = sum(input_window) / len(input_window)
        baseline_cost = min(observed_cost, _BASELINE_COST_CEILING)
        baseline_input = min(observed_input, float(_BASELINE_INPUT_CEILING))

    cost_triggered = cost_usd > _COST_TRIGGER_MULTIPLE * baseline_cost
    io_triggered = (
        input_tokens > _IO_FINGERPRINT_INPUT_MULTIPLE * baseline_input
        and output_tokens < _IO_FINGERPRINT_OUTPUT_CEILING
    )
    input_absolute_triggered = input_tokens > _ABSOLUTE_INPUT_TOKENS_FLOOR
    absolute_triggered = cost_usd > _ABSOLUTE_COST_USD_FLOOR
    any_triggered = cost_triggered or io_triggered or input_absolute_triggered or absolute_triggered
    if not any_triggered:
        return

    # Priority order (decision C, extended by 61-followup-D §B):
    # io_fingerprint > input_absolute > cost_multiple > cost_absolute.
    if io_triggered:
        trigger = "io_fingerprint"
    elif input_absolute_triggered:
        trigger = "input_absolute"
    elif cost_triggered:
        trigger = "cost_multiple"
    else:
        trigger = "cost_absolute"
    fields: dict[str, Any] = {
        "trigger": trigger,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "baseline_cost_usd": baseline_cost,
        "baseline_input_tokens": baseline_input,
        "warmup": warmup,
        "model": model,
        "session_id": session_id,
        # Story 91-4: caller discriminator for cross-model attribution.
        "caller": caller,
    }
    logger.error(
        "narrator.cost_runaway_suspected trigger=%s input=%d "
        "output=%d cost_usd=%.6f baseline_cost_usd=%.6f "
        "baseline_input_tokens=%.1f warmup=%s model=%s "
        "session_id=%s caller=%s",
        trigger,
        input_tokens,
        output_tokens,
        cost_usd,
        baseline_cost,
        baseline_input,
        warmup,
        model,
        session_id,
        caller,
    )
    _watcher_publish_event(
        "cost_runaway_suspected",
        fields,
        component="narrator.sdk",
        severity="warn",
    )


class SessionCostLedger:
    """Process-level per-session cost state shared by every call site.

    State grows one entry per distinct ``session_id`` for the process
    lifetime (the ADR-122 never-evict trade-off the narrator client
    already documents); ``reset_baselines`` is the per-session eviction
    handle for the adapter windows, mirroring
    ``AnthropicSdkClient.reset_baselines``'s scope (baselines only — the
    cumulative/announce state is intentionally NOT cleared there either,
    per the ADR-134 flagged follow-up).
    """

    def __init__(self) -> None:
        # Shared with AnthropicSdkClient instances by reference (their
        # constructor aliases these two onto the instance attrs the
        # 61-followup-D machinery reads/writes).
        self.cumulative_cost_usd: dict[str, float] = {}
        self.ceiling_announced: set[str] = set()
        # Adapter-side rolling baselines, keyed (session_id, caller) —
        # the per-call-site shape separation that keeps Haiku traffic
        # from training narrator baselines and vice versa.
        self._cost_baseline: dict[tuple[str, str], deque[float]] = {}
        self._input_tokens_baseline: dict[tuple[str, str], deque[int]] = {}

    # -- ceiling -------------------------------------------------------

    def check_ceiling(self, session_id: str, *, ceiling_usd: float) -> None:
        """Pre-flight refusal: raise if the session already crossed the
        ceiling on ANY call site's prior call (terminal, no recovery)."""
        cumulative = self.cumulative_cost_usd.get(session_id, 0.0)
        if cumulative >= ceiling_usd:
            raise build_ceiling_exceeded(
                session_id=session_id, cumulative=cumulative, ceiling_usd=ceiling_usd
            )

    def update_cumulative(
        self,
        *,
        session_id: str,
        cost_usd: float,
        model: str,
        ceiling_usd: float,
    ) -> None:
        """Add a billed call's cost; on crossing, emit the typed
        ``session.cost_ceiling_exceeded`` event (once per session) and
        raise. The call that crossed has already billed Anthropic — the
        kill is "no further calls" (61-followup-D §C, relocated by 91-4
        so adapter spend feeds the same pot)."""
        cumulative = self.cumulative_cost_usd.get(session_id, 0.0) + cost_usd
        self.cumulative_cost_usd[session_id] = cumulative

        if cumulative < ceiling_usd:
            return
        if session_id in self.ceiling_announced:
            raise build_ceiling_exceeded(
                session_id=session_id, cumulative=cumulative, ceiling_usd=ceiling_usd
            )

        logger.error(
            "session.cost_ceiling_exceeded session_id=%s "
            "cumulative_cost_usd=%.6f ceiling_usd=%.2f model=%s",
            session_id,
            cumulative,
            ceiling_usd,
            model,
        )
        _watcher_publish_event(
            "session.cost_ceiling_exceeded",
            {
                "session_id": session_id,
                "cumulative_cost_usd": cumulative,
                "ceiling_usd": ceiling_usd,
                "model": model,
            },
            component="narrator.sdk",
            severity="error",
        )
        # Announce-set add AFTER the side-effecting emit (lang-review §14;
        # Reviewer 2026-05-23 finding) — a failed emit must not poison the
        # set and permanently lose the GM-panel event on retry.
        self.ceiling_announced.add(session_id)
        raise build_ceiling_exceeded(
            session_id=session_id, cumulative=cumulative, ceiling_usd=ceiling_usd
        )

    # -- detector (adapter call sites) -----------------------------------

    def record_call(
        self,
        *,
        session_id: str,
        caller: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        ceiling_usd: float,
    ) -> None:
        """Full post-call safety pass for an adapter call: detector check
        against the (session, caller) rolling baselines, baseline append,
        then cumulative update + ceiling enforcement. Mirrors the
        narrator's per-iter ordering in ``complete_with_tools``
        (runaway check → append → cumulative)."""
        key = (session_id, caller)
        check_and_emit_runaway(
            cost_window=self._cost_baseline.get(key),
            input_window=self._input_tokens_baseline.get(key),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model=model,
            session_id=session_id,
            caller=caller,
        )
        # Append AFTER the check so the comparator always sees PRIOR
        # observations (61-followup-A contract).
        self._cost_baseline.setdefault(key, deque(maxlen=_BASELINE_WINDOW_K)).append(cost_usd)
        self._input_tokens_baseline.setdefault(key, deque(maxlen=_BASELINE_WINDOW_K)).append(
            input_tokens
        )
        self.update_cumulative(
            session_id=session_id,
            cost_usd=cost_usd,
            model=model,
            ceiling_usd=ceiling_usd,
        )

    # -- eviction / test isolation ---------------------------------------

    def instrumented_total_usd(self) -> float:
        """Sum all per-session cumulative costs — the Layer 1 figure for the
        dark-spend reconciliation (story 91-5). Returns 0.0 on an empty ledger."""
        return sum(self.cumulative_cost_usd.values())

    def reset_baselines(self, session_id: str) -> None:
        """Drop the adapter rolling baselines for one session (the
        ``SessionRoom.close_store()`` eviction handle, extended to the
        adapter windows by 91-4). Cumulative/announce state is
        intentionally NOT cleared — same scope as
        ``AnthropicSdkClient.reset_baselines`` (ADR-134 flagged
        follow-up)."""
        for store in (self._cost_baseline, self._input_tokens_baseline):
            for key in [k for k in store if k[0] == session_id]:
                store.pop(key, None)

    def reset_for_tests(self) -> None:
        """Clear ALL ledger state — autouse test-isolation hook only
        (``tests/conftest.py``). Never called from production code: the
        ledger's process lifetime IS the contract there."""
        self.cumulative_cost_usd.clear()
        self.ceiling_announced.clear()
        self._cost_baseline.clear()
        self._input_tokens_baseline.clear()


_LEDGER = SessionCostLedger()


def ledger() -> SessionCostLedger:
    """The process-level ledger singleton every call site shares."""
    return _LEDGER
