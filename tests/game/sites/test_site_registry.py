"""SiteRegistry + namespacing unit tests (Track B, Task 1 — story 164-1).

RED-phase tests. Until Dev creates the ``sidequest/game/sites/`` package and
adds the ``CartographyConfig.sites`` field, these fail at import with
``ModuleNotFoundError: sidequest.game.sites`` (and ``.sites`` is silently
dropped by ``CartographyConfig``'s ``extra="ignore"`` until the typed field
lands, so ``_cart().sites`` would be an ``AttributeError``).

Scope note (AC-12 wiring): Task 1 is deliberately *pure/additive* — the
registry is consumed by the movement/map-emit layers in LATER stories
(164-2/3/4) per the plan's risk-sequencing (Sünden must stay green at every
merge). The honest in-scope "wiring" here is that ``CartographyConfig.sites``
is the *real* production model field that ``SiteRegistry.from_cartography``
consumes — proven by ``test_cartography_config_sites_wire_into_registry``,
which builds the registry from a genuine ``CartographyConfig`` (no mock).
Dispatch-path wiring tests belong to the consuming stories.
"""

from __future__ import annotations

import dataclasses

import pytest

from sidequest.game.sites import (
    SiteDecl,
    SiteDescriptor,
    SiteRegistry,
    is_site_node_id,
    site_entrance_id,
    site_id_of,
)
from sidequest.genre.models.world import CartographyConfig


def _region(name: str, adjacent: list[str] | None = None) -> dict:
    """A minimal VALID Region dict. ``Region`` requires ``summary`` and
    ``description``; the registry only reads ``adjacent``, so those fields are
    filled with the name to keep the fixture terse but schema-valid."""
    return {"name": name, "summary": name, "description": name, "adjacent": adjacent or []}


def _cart() -> CartographyConfig:
    """A world with two sites: a frontier deep owned by the_dropmouth (reachable
    from the adjacent ropefoot camp) and a bounded tavern owned by the square."""
    return CartographyConfig.model_validate(
        {
            "navigation_mode": "region",
            "regions": {
                "ropefoot": _region("Ropefoot Camp", ["the_dropmouth"]),
                "the_dropmouth": _region("The Dropmouth", ["ropefoot"]),
                "square": _region("Village Square"),
            },
            "sites": [
                {
                    "site_id": "frontier",
                    "name": "The Deep",
                    "archetype": "megadungeon",
                    "attached_to": "the_dropmouth",
                    "extent": "frontier",
                },
                {
                    "site_id": "gilded_boar",
                    "name": "The Gilded Boar",
                    "archetype": "tavern",
                    "attached_to": "square",
                    "extent": "bounded",
                },
            ],
        }
    )


def _cart_multi() -> CartographyConfig:
    """A hub node that OWNS one site and REACHES a second via an adjacent node —
    for owner-first ordering and empty-descriptor-ambiguity checks."""
    return CartographyConfig.model_validate(
        {
            "navigation_mode": "region",
            "regions": {
                "hub": _region("Hub", ["annex"]),
                "annex": _region("Annex", ["hub"]),
            },
            "sites": [
                {
                    "site_id": "owned",
                    "name": "Owned Hall",
                    "archetype": "tavern",
                    "attached_to": "hub",
                },
                {
                    "site_id": "reachable",
                    "name": "Reachable Vault",
                    "archetype": "vault",
                    "attached_to": "annex",
                },
            ],
        }
    )


# --------------------------------------------------------------------------
# AC-1: pure/additive — zero behavior change when ``sites`` is empty
# --------------------------------------------------------------------------


def test_sites_field_defaults_empty() -> None:
    # A world that declares no sites gets an empty list (no behavior change).
    assert CartographyConfig().sites == []


def test_sites_field_parses_declared_sites() -> None:
    assert len(_cart().sites) == 2


def test_from_cartography_on_empty_config_is_inert() -> None:
    reg = SiteRegistry.from_cartography(CartographyConfig())
    assert reg.sites_for_node("anywhere") == []
    assert reg.by_id("frontier") is None
    assert reg.resolve_descriptor("anywhere", "the deep") == (None, False)


def test_from_cartography_none_is_defensive_empty() -> None:
    # ``from_cartography(None)`` must not raise — it returns an inert registry.
    reg = SiteRegistry.from_cartography(None)
    assert reg.sites_for_node("x") == []
    assert reg.by_id("frontier") is None


# --------------------------------------------------------------------------
# AC-2: indexes sites by owner and adjacency
# --------------------------------------------------------------------------


def test_sites_for_node_includes_owner() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    # the_dropmouth OWNS the frontier site; square owns the tavern.
    assert [s.site_id for s in reg.sites_for_node("the_dropmouth")] == ["frontier"]
    assert [s.site_id for s in reg.sites_for_node("square")] == ["gilded_boar"]


def test_sites_for_node_includes_adjacent_owner() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    # ropefoot is ADJACENT to the owner -> the deep is enterable from the camp.
    assert [s.site_id for s in reg.sites_for_node("ropefoot")] == ["frontier"]


def test_sites_for_node_owner_precedes_adjacent() -> None:
    reg = SiteRegistry.from_cartography(_cart_multi())
    # hub OWNS 'owned' and REACHES 'reachable' via annex; owner comes first.
    assert [s.site_id for s in reg.sites_for_node("hub")] == ["owned", "reachable"]


def test_sites_for_node_dedups_repeated_adjacency() -> None:
    # An adjacency list that names the same owner twice must not double-count.
    cart = CartographyConfig.model_validate(
        {
            "navigation_mode": "region",
            "regions": {
                "camp": _region("Camp", ["rim", "rim"]),
                "rim": _region("Rim", ["camp"]),
            },
            "sites": [
                {
                    "site_id": "deep",
                    "name": "The Deep",
                    "archetype": "megadungeon",
                    "attached_to": "rim",
                    "extent": "frontier",
                },
            ],
        }
    )
    reg = SiteRegistry.from_cartography(cart)
    assert [s.site_id for s in reg.sites_for_node("camp")] == ["deep"]


def test_sites_for_node_unknown_node_is_empty() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    assert reg.sites_for_node("nowhere") == []


def test_by_id_hit_and_miss() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    hit = reg.by_id("gilded_boar")
    assert hit is not None and hit.name == "The Gilded Boar"
    assert reg.by_id("does_not_exist") is None


# --------------------------------------------------------------------------
# AC-3: descriptor resolution disambiguates by name / id
# --------------------------------------------------------------------------


def test_resolve_descriptor_by_full_name() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    site, ambiguous = reg.resolve_descriptor("square", "the gilded boar")
    assert site is not None and site.site_id == "gilded_boar" and ambiguous is False


def test_resolve_descriptor_by_id_substring() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    site, ambiguous = reg.resolve_descriptor("square", "gilded")
    assert site is not None and site.site_id == "gilded_boar" and ambiguous is False


def test_resolve_descriptor_unmatched_is_none_not_ambiguous() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    miss, ambiguous = reg.resolve_descriptor("square", "the moon")
    assert miss is None and ambiguous is False


def test_resolve_descriptor_no_candidates_returns_none_false() -> None:
    reg = SiteRegistry.from_cartography(CartographyConfig())
    assert reg.resolve_descriptor("square", "anything") == (None, False)


def test_resolve_descriptor_empty_descriptor_sole_candidate_resolves() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    # square has exactly ONE enterable site -> empty descriptor still resolves.
    site, ambiguous = reg.resolve_descriptor("square", "")
    assert site is not None and site.site_id == "gilded_boar" and ambiguous is False


def test_resolve_descriptor_empty_descriptor_multi_candidate_is_ambiguous() -> None:
    reg = SiteRegistry.from_cartography(_cart_multi())
    # hub reaches two sites -> an empty descriptor cannot disambiguate.
    site, ambiguous = reg.resolve_descriptor("hub", "")
    assert site is None and ambiguous is True


def test_resolve_descriptor_ambiguous_name_match() -> None:
    # Two sites whose names share a substring -> a matching descriptor is ambiguous.
    cart = CartographyConfig.model_validate(
        {
            "navigation_mode": "region",
            "regions": {"crossroads": _region("Crossroads")},
            "sites": [
                {
                    "site_id": "old_mill",
                    "name": "The Old Mill",
                    "archetype": "tavern",
                    "attached_to": "crossroads",
                },
                {
                    "site_id": "old_forge",
                    "name": "The Old Forge",
                    "archetype": "vault",
                    "attached_to": "crossroads",
                },
            ],
        }
    )
    reg = SiteRegistry.from_cartography(cart)
    site, ambiguous = reg.resolve_descriptor("crossroads", "old")
    assert site is None and ambiguous is True


# --------------------------------------------------------------------------
# AC-4: namespacing helpers + site_owning_node round-trip
# --------------------------------------------------------------------------


def test_site_owning_node_maps_namespaced_id_back() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    owner = reg.site_owning_node("gilded_boar:r2")
    assert owner is not None and owner.site_id == "gilded_boar"


def test_site_owning_node_bare_id_is_none() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    assert reg.site_owning_node("the_dropmouth") is None


def test_site_owning_node_unknown_site_is_none() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    # namespaced but the site id is not registered.
    assert reg.site_owning_node("ghost_keep:r1") is None


def test_site_entrance_id() -> None:
    assert site_entrance_id("gilded_boar") == "gilded_boar:entrance"


def test_is_site_node_id_true_cases() -> None:
    assert is_site_node_id("gilded_boar:r2") is True
    assert is_site_node_id("gilded_boar:exp003.r1") is True


def test_is_site_node_id_false_cases() -> None:
    assert is_site_node_id("the_dropmouth") is False
    assert is_site_node_id("") is False


def test_site_id_of() -> None:
    assert site_id_of("gilded_boar:entrance") == "gilded_boar"
    assert site_id_of("the_dropmouth") is None
    assert site_id_of("") is None


# --------------------------------------------------------------------------
# SiteDescriptor type-design invariants (frozen runtime view, extent default)
# --------------------------------------------------------------------------


def test_site_descriptor_entrance_node_id_property() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    boar = reg.by_id("gilded_boar")
    assert boar is not None and boar.entrance_node_id == "gilded_boar:entrance"


def test_site_descriptor_is_frozen() -> None:
    reg = SiteRegistry.from_cartography(_cart())
    boar = reg.by_id("gilded_boar")
    assert boar is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        boar.site_id = "hacked"  # type: ignore[misc]  # frozen runtime view


def test_site_extent_defaults_bounded() -> None:
    cart = CartographyConfig.model_validate(
        {
            "navigation_mode": "region",
            "regions": {"square": _region("Square")},
            "sites": [
                {
                    "site_id": "inn",
                    "name": "The Inn",
                    "archetype": "tavern",
                    "attached_to": "square",
                },
            ],
        }
    )
    reg = SiteRegistry.from_cartography(cart)
    inn = reg.by_id("inn")
    assert inn is not None and inn.extent == "bounded"


# --------------------------------------------------------------------------
# AC-12 (in-scope wiring): the registry consumes the REAL CartographyConfig
# model field — not a mock. Dispatch-path wiring is deferred to 164-2/3/4.
# --------------------------------------------------------------------------


def test_cartography_config_sites_wire_into_registry() -> None:
    cart = _cart()
    # The field type is the production SiteDecl (pydantic-parsed, not a raw dict).
    assert cart.sites and all(isinstance(s, SiteDecl) for s in cart.sites)
    reg = SiteRegistry.from_cartography(cart)
    # Every declared site is indexed and resolves to a runtime SiteDescriptor.
    for decl in cart.sites:
        desc = reg.by_id(decl.site_id)
        assert isinstance(desc, SiteDescriptor)
        assert desc.attached_to == decl.attached_to
