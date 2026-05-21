"""OTEL span name catalog (package).

Public API. Per-domain submodules declare their constants, register their
``SPAN_ROUTES`` entries, add to ``FLAT_ONLY_SPANS``, and export their helper
context managers / event emitters. This module re-exports them so that
``from sidequest.telemetry.spans import SPAN_X, foo_span`` keeps working
across the package split.

Adding a new domain:
  1. Create ``spans/<domain>.py`` with constants + helpers.
  2. Add a ``from .<domain> import *`` line below.
  3. Tests/test_routing_completeness.py validates that every constant is
     either routed (in ``SPAN_ROUTES``) or flat-only (in ``FLAT_ONLY_SPANS``).
"""

from __future__ import annotations

# Re-export ``tracer`` so test fixtures can monkeypatch
# ``sidequest.telemetry.spans.tracer`` to install in-memory exporters.
# ``Span.open`` reads it via late import to honour the patched binding.
from sidequest.telemetry.setup import tracer  # noqa: F401

from ._core import (  # noqa: F401
    FLAT_ONLY_SPANS,
    SPAN_ROUTES,
    SpanRoute,
    _SpanLike,
)

# Domain submodules. Star-import order is registry-insertion order — keep
# this list in sync with `tests/telemetry/test_routing_completeness.py`.
from .agent import *  # noqa: F401, F403
from .aside import *  # noqa: F401, F403
from .asset_url import *  # noqa: F401, F403
from .audio import *  # noqa: F401, F403
from .barrier import *  # noqa: F401, F403
from .catch_up import *  # noqa: F401, F403
from .cavern_room import *  # noqa: F401, F403
from .chargen import *  # noqa: F401, F403
from .chart import *  # noqa: F401, F403
from .clock import *  # noqa: F401, F403
from .combat import *  # noqa: F401, F403
from .compose import *  # noqa: F401, F403
from .confrontation_intent import *  # noqa: F401, F403
from .content import *  # noqa: F401, F403
from .continuity import *  # noqa: F401, F403
from .cookbook import *  # noqa: F401, F403
from .course import *  # noqa: F401, F403
from .dice import *  # noqa: F401, F403
from .disposition import *  # noqa: F401, F403
from .dogfight import *  # noqa: F401, F403
from .dungeon_attach import *  # noqa: F401, F403
from .dungeon_materialize import *  # noqa: F401, F403
from .dungeon_persist import *  # noqa: F401, F403
from .dungeon_region_projection import *  # noqa: F401, F403
from .dungeon_render import *  # noqa: F401, F403
from .dungeon_setpiece import *  # noqa: F401, F403
from .emitter import Emitter  # noqa: F401
from .encounter import *  # noqa: F401, F403
from .interior import *  # noqa: F401, F403
from .inventory import *  # noqa: F401, F403
from .journal import *  # noqa: F401, F403
from .lobby import *  # noqa: F401, F403
from .local_dm import *  # noqa: F401, F403
from .location import *  # noqa: F401, F403
from .lore import *  # noqa: F401, F403
from .magic import *  # noqa: F401, F403
from .merchant import *  # noqa: F401, F403
from .monster_manual import *  # noqa: F401, F403
from .mp import *  # noqa: F401, F403
from .namegen import *  # noqa: F401, F403
from .narrator import *  # noqa: F401, F403
from .narrator_streaming import *  # noqa: F401, F403
from .npc import *  # noqa: F401, F403
from .opening import *  # noqa: F401, F403
from .orchestrator import *  # noqa: F401, F403
from .persistence import *  # noqa: F401, F403
from .pregen import *  # noqa: F401, F403
from .projection import *  # noqa: F401, F403
from .prompt import *  # noqa: F401, F403
from .rag import *  # noqa: F401, F403
from .recent_narrative import *  # noqa: F401, F403
from .region_state import *  # noqa: F401, F403
from .reminder import *  # noqa: F401, F403
from .render import *  # noqa: F401, F403
from .rig import *  # noqa: F401, F403
from .room_state import *  # noqa: F401, F403
from .scenario import *  # noqa: F401, F403
from .scrapbook import *  # noqa: F401, F403
from .script_tool import *  # noqa: F401, F403
from .span import Span  # noqa: F401
from .state_patch import *  # noqa: F401, F403
from .trope import *  # noqa: F401, F403
from .turn import *  # noqa: F401, F403
from .world import *  # noqa: F401, F403
from .world_grounding import *  # noqa: F401, F403
from .world_grounding import (  # noqa: F401
    emit_demographics_injected_span,
    emit_weather_proposed_span,
    emit_weather_used_span,
)
