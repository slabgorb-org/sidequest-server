"""Fixtures for integration tests.

Re-exports fixtures from ``tests.server.conftest`` so integration tests can
build a real ``WebSocketSessionHandler`` + ``_SessionData`` and drive
encounter engine paths without re-implementing the fixtures.
"""

from __future__ import annotations

from tests.server.conftest import (  # noqa: F401
    encounter_dispatch_helper,
    otel_capture,
    session_fixture,
    session_handler_factory,
    store_bound_to_hub,
    synthetic_two_dial_pack,
)


def make_minimal_coyote_star_magic_state():
    """Build a minimum-valid MagicState for the coyote_star test world.

    S1 invariant (2026-05-04 split-brain cleanup): magic_state must be
    initialized before ``init_chassis_registry`` runs, because the chassis
    loader writes confrontations into ``snapshot.magic_state.confrontations``
    directly. Tests that previously called ``init_chassis_registry`` with
    a None ``magic_state`` need this helper.
    """
    from sidequest.magic.models import WorldKnowledge, WorldMagicConfig
    from sidequest.magic.state import MagicState

    return MagicState.from_config(
        WorldMagicConfig(
            world_slug="coyote_star",
            genre_slug="space_opera",
            allowed_sources=[],
            active_plugins=[],
            intensity=0.0,
            world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
            visibility={"primary": "feared", "local_register": "dismissed"},
            hard_limits=[],
            cost_types=[],
            ledger_bars=[],
            narrator_register="test",
        )
    )
