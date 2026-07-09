"""Track B, Task 5 (Story 164-3): the router state summary surfaces ENTERABLE sites.

RED: ``_build_state_summary`` does not yet emit ``current_sites`` and the router
``_SYSTEM_PROMPT`` does not yet document the ``enter_site`` / ``exit_site`` param
shapes. The intent router can only classify a "go into the tavern / down into the
deep" utterance as an ``enter_site`` movement if it is (1) TOLD which
sub-locations are enterable from the PC's current region and (2) taught the param
vocabulary — the same lexical-bridge principle that seeded ``current_region_exits``
(105-2: authored vocabulary beats inference). Dev wires ``current_sites`` from a
``SiteRegistry`` and extends the prompt in GREEN (Task 5).

Pure CODE tests: they drive the REAL production ``_build_state_summary`` (the
function the router pass calls at ``intent_router_pass.py`` ~:965) with a
synthetic beneath_sünden-shaped pack that declares a ``frontier`` site. They
assert the summary STRUCTURE the router consumes — not a content invariant (that
the real beneath_sunden pack declares the site is the pack validator's / the
``sunden_descend_trace`` scenario's job, per no-content-in-unit-tests).
"""

from __future__ import annotations

import types

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
    SiteDecl,
)
from sidequest.server.intent_router_pass import _build_state_summary


def _sunden_cart() -> CartographyConfig:
    """beneath_sünden-shaped: region-mode, ``the_dropmouth`` owns the descent and
    declares the ``frontier`` site; ``ropefoot`` (the camp) is one step adjacent.
    The (now-inert) ``deep_descent`` route is kept, mirroring the real pack."""
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="Surface camp.",
                description="The waiting camp above the shaft.",
                adjacent=["the_dropmouth"],
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
                adjacent=["ropefoot"],
            ),
        },
        routes=[
            Route(
                name="Down the Rope",
                description="The one-way descent.",
                from_id="the_dropmouth",
                to_id="deep_descent",
            ),
        ],
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="the_dropmouth",
                extent="frontier",
            ),
        ],
    )


def _pack(world_slug: str, cart: CartographyConfig):
    """Duck-typed GenrePack exposing only what ``_build_state_summary`` reads
    (mirrors ``test_intent_router_region_exits.py``): ``rules=None`` skips the
    confrontation block, ``witnessed_acts=None`` skips the witnessed-act block,
    and ``worlds[slug].cartography`` feeds the region-exits + sites projection."""
    world = types.SimpleNamespace(cartography=cart)
    return types.SimpleNamespace(rules=None, witnessed_acts=None, worlds={world_slug: world})


def _snapshot(region: str, *, world_slug: str = "beneath_sunden") -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug=world_slug,
        pc_regions={"Rux": region},
        player_seats={"p1": "Rux"},
    )


def test_state_summary_lists_enterable_site_at_owner_region() -> None:
    """A PC on the seam-owner region (``the_dropmouth``) sees the frontier site in
    ``current_sites`` — site_id/name/archetype — the router's cue to emit
    ``enter_site`` for "down into the deep"."""
    summary = _build_state_summary(
        _snapshot("the_dropmouth"),
        pack=_pack("beneath_sunden", _sunden_cart()),
        acting_player="Rux",
    )
    sites = summary.get("current_sites")
    assert sites, f"current_sites absent — router cannot cue enter_site: keys={list(summary)}"
    assert any(
        s["site_id"] == "frontier" and s["name"] == "The Deep" and s["archetype"] == "megadungeon"
        for s in sites
    ), sites


def test_state_summary_lists_enterable_site_from_adjacent_camp() -> None:
    """A PC one step off the owner (``ropefoot``, adjacent to ``the_dropmouth``)
    STILL sees the frontier site — the 'down the rope at the camp' one-action
    reach that ``SiteRegistry.sites_for_node`` preserves via adjacency."""
    summary = _build_state_summary(
        _snapshot("ropefoot"),
        pack=_pack("beneath_sunden", _sunden_cart()),
        acting_player="Rux",
    )
    assert any(s["site_id"] == "frontier" for s in summary.get("current_sites", [])), (
        f"adjacent camp lost the enterable site: {summary.get('current_sites')}"
    )


def test_state_summary_omits_current_sites_when_none_enterable() -> None:
    """Negative guard: a region with no owned/adjacent site emits NO ``current_sites``
    key — the router payload stays free of empty-list noise (mirrors how
    ``current_region_exits`` is only set when non-empty)."""
    cart = CartographyConfig(
        starting_region="market",
        navigation_mode=NavigationMode.region,
        regions={
            "market": Region(name="Market", summary="s", description="d", adjacent=[]),
        },
        routes=[],
        sites=[],
    )
    summary = _build_state_summary(
        _snapshot("market", world_slug="oz"),
        pack=_pack("oz", cart),
        acting_player="Rux",
    )
    assert "current_sites" not in summary, summary


def test_system_prompt_documents_site_action_vocabulary() -> None:
    """The router system prompt must teach the ``enter_site`` / ``exit_site`` param
    shapes; without the vocabulary the classifier never emits them even when
    ``current_sites`` is present. Asserts the prompt DATA (the instruction sent to
    the classifier), not source text."""
    from sidequest.agents.intent_router import _SYSTEM_PROMPT

    assert "enter_site" in _SYSTEM_PROMPT, "router prompt does not document enter_site"
    assert "exit_site" in _SYSTEM_PROMPT, "router prompt does not document exit_site"
