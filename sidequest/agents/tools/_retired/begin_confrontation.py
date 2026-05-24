"""begin_confrontation — RETIRED Story 59-4 (ADR-113).

This tool was the SDK narrator's confrontation engagement signal
introduced in Story 59-1: the narrator called it, the SDK assembler
at ``_assemble_turn_result_sdk`` (orchestrator.py) lifted the
requested type onto ``result.confrontation``, and
``narration_apply.py`` instantiated the encounter on the canonical
snapshot. The mechanism worked, but it left the NARRATOR owning
engagement signaling — the "convincing prose with no mechanical
backing" SOUL Illusionism failure mode the Epic 59 reframe
(Houlihan 2026-05-23) exists to eliminate.

ADR-113 introduced the Intent Router engagement spine: a pre-narrator
pass that reads each player action, classifies intent via Haiku as
confidence-scored advisory dispatches, and engages mechanical engines
on the canonical snapshot BEFORE the narrator runs. The narrator then
narrates already-real state instead of self-reporting engagement.

Story 59-4 (atomic, no parallel window per memory rule
``feedback_one_mechanism_per_problem``) cut the SDK confrontation
engagement path over to the spine. Where the narrator used to call
``begin_confrontation``, the router now produces a
``SubsystemDispatch(subsystem="confrontation", params={"type": ...})``
and the dispatch bank invokes the live engager at:

    sidequest/agents/subsystems/confrontation.py
        :: run_confrontation_dispatch

which calls the same ``instantiate_encounter_from_trigger`` helper
``narration_apply.py``'s consumer used to call — single creation path,
moved from post-narrator to pre-narrator.

The confrontation engagement criteria the narrator used to read from
this tool's description (ADR-111) now live on the Intent Router's
Haiku system prompt at ``sidequest/agents/intent_router.py::_SYSTEM_PROMPT``.

This module deliberately exposes NO callable symbol — importing it as a
runtime shim would silently no-op engagement and violate memory rule
``feedback_no_fallbacks_hard``. Importing
``sidequest.agents.tools.begin_confrontation`` (the original path) now
raises ``ImportError`` because the original module is gone. A grep
for ``begin_confrontation`` lands here, in this retired stub, with
this docstring as the breadcrumb to the live mechanism.

Do not add code below this docstring. Do not re-register this as a
tool. Do not import this from production code.
"""
