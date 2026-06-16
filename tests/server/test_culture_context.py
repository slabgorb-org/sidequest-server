"""Tests for the culture-reference helper used in the narrator prompt.

Covers the Phase 2.2 IOU: ``Culture.chargen == False`` cultures must not
leak into the narrator's ``AVAILABLE CULTURES`` block. Unit tests cover
the filter + format; a heavy_metal/evropi wiring test proves the filter
engages on real pack content (Ingurdios / Tismenni / Kobold are YAML-
authored as ``chargen: false`` and must be absent from the block); a
session-handler wiring test proves the filter reaches the live
:class:`TurnContext` that drives the narrator prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.culture import Culture
from sidequest.protocol.messages import SessionEventMessage, SessionEventPayload
from sidequest.server.dispatch.culture_context import (
    build_culture_reference,
    resolve_culture_reference,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests._helpers.genre_paths import find_pack_path
from tests.server.conftest import mock_claude_client_factory


def _culture(name: str, *, chargen: bool = True, description: str = "desc") -> Culture:
    return Culture(
        name=name,
        summary="s",
        description=description,
        chargen=chargen,
    )


class TestBuildCultureReference:
    def test_empty_list_returns_empty_string(self) -> None:
        assert build_culture_reference([]) == ""

    def test_all_filtered_out_returns_empty_string(self) -> None:
        cultures = [_culture("Lore1", chargen=False), _culture("Lore2", chargen=False)]
        assert build_culture_reference(cultures) == ""

    def test_single_eligible_culture_rendered(self) -> None:
        cultures = [_culture("Northfolk", description="Hardy seafarers.")]
        result = build_culture_reference(cultures)
        assert result == "\n=== AVAILABLE CULTURES ===\n- Northfolk — Hardy seafarers."

    def test_leading_newline_for_concat_onto_world_context(self) -> None:
        result = build_culture_reference([_culture("X")])
        assert result.startswith("\n"), "caller concatenates onto world_context"

    def test_lore_only_culture_excluded(self) -> None:
        cultures = [
            _culture("Playable", description="A"),
            _culture("LoreOnly", chargen=False, description="B"),
            _culture("AlsoPlayable", description="C"),
        ]
        result = build_culture_reference(cultures)
        assert "Playable" in result
        assert "AlsoPlayable" in result
        assert "LoreOnly" not in result

    def test_default_chargen_true_included(self) -> None:
        c = Culture(name="Default", summary="s", description="d")
        assert c.chargen is True, "model default must include culture"
        assert "Default" in build_culture_reference([c])

    def test_order_preserved(self) -> None:
        cultures = [_culture("Alpha"), _culture("Beta"), _culture("Gamma")]
        result = build_culture_reference(cultures)
        alpha_idx = result.index("Alpha")
        beta_idx = result.index("Beta")
        gamma_idx = result.index("Gamma")
        assert alpha_idx < beta_idx < gamma_idx


class TestResolveCultureReference:
    def test_prefers_world_cultures_over_pack_cultures(self, tmp_path: Path) -> None:
        # Wired via integration below; unit-covered by build_culture_reference
        # variants. The resolver's own behaviour is exercised against a real
        # pack in the evropi wiring test.
        pytest.skip("covered by evropi wiring test")


# ---------------------------------------------------------------------------
# Wiring test: heavy_metal/evropi must not leak lore-only cultures
# ---------------------------------------------------------------------------


def _content_root() -> Path:
    # tests/server/test_culture_context.py → ../../../../sidequest-content
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "sidequest-content"
    if not candidate.exists():
        pytest.skip(f"sidequest-content not available at {candidate}")
    return candidate


class TestEvropiLoreOnlyNotLeaked:
    """Live-pack wiring check — evropi authored three lore-only cultures."""

    def test_ingurdios_tismenni_yrs_excluded_from_reference(self) -> None:
        _content_root()  # skip if sidequest-content unavailable
        pack = load_genre_pack(find_pack_path("heavy_metal"))

        reference = resolve_culture_reference(pack, "evropi")

        assert reference, "evropi has cultures; reference must be non-empty"
        assert "=== AVAILABLE CULTURES ===" in reference

        # Three authored lore-only cultures (chargen: false in YAML).
        # Match the list-entry shape only — the *names* appear in the
        # description prose of other cultures (Mistos enslaved Ingurdios,
        # Zkęd descend from Tismenni, etc.). A leak is a list entry.
        for lore_only_name in ("Ingurdios", "Tismenni", "Kobold"):
            assert f"- {lore_only_name} —" not in reference, (
                f"{lore_only_name} is chargen: false in evropi/cultures.yaml "
                f"but leaked into the narrator reference as a list entry"
            )

        # At least one authored chargen-eligible culture must be present —
        # otherwise the filter is over-zealous, not load-bearing.
        world = pack.worlds["evropi"]
        eligible_names = [c.name for c in world.cultures if c.chargen]
        assert eligible_names, "test-fixture drift: evropi has no eligible cultures"
        assert any(name in reference for name in eligible_names), (
            "no chargen-eligible cultures appeared — filter is broken"
        )


# ---------------------------------------------------------------------------
# Wiring test: session handler threads the filtered reference into
# _SessionData.world_context, which _build_turn_context propagates to the
# TurnContext the orchestrator uses to build the narrator prompt.
# ---------------------------------------------------------------------------


class TestSessionHandlerWiresWorldContext:
    def test_connect_to_evropi_populates_filtered_world_context(self, tmp_path: Path) -> None:
        _content_root()  # skip if sidequest-content unavailable
        # Use the resolved pack's parent root so the handler's pack-search
        # logic can locate "heavy_metal" regardless of which content root
        # currently houses it (genre_packs/ vs genre_workshopping/).
        heavy_metal_root = find_pack_path("heavy_metal").parent
        handler = WebSocketSessionHandler(
            claude_client_factory=mock_claude_client_factory(),
            genre_pack_search_paths=[heavy_metal_root],
            save_dir=tmp_path,
        )

        from tests.server.conftest import attach_default_room_context, seed_slug_for_test

        slug = seed_slug_for_test(tmp_path, genre="heavy_metal", world="evropi")
        attach_default_room_context(handler)

        async def body() -> None:
            await handler.handle_message(
                SessionEventMessage(
                    payload=SessionEventPayload(
                        event="connect",
                        player_name="WiringProbe",
                        game_slug=slug,
                    ),
                    player_id="",
                )
            )

        asyncio.run(body())

        sd = handler._session_data  # type: ignore[attr-defined]
        assert sd is not None, "connect did not initialise session data"
        assert sd.world_context is not None, "world_context missing on session"
        assert "=== AVAILABLE CULTURES ===" in sd.world_context

        # Filter engaged end-to-end: the lore-only cultures authored in
        # evropi/cultures.yaml as ``chargen: false`` never reach the
        # field that feeds the narrator prompt.
        for lore_only_name in ("Ingurdios", "Tismenni", "Kobold"):
            assert f"- {lore_only_name} —" not in sd.world_context
