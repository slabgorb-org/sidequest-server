"""Failing tests for Story 75-11 — the ``is_projectable()`` predicate
(ADR-138 §D1/D3).

ADR-138 §D3 defines a single projection-eligibility predicate so that no
downstream surface (ADR-118 retrieval index, ADR-135 reference page) re-implements
the ratification rule::

    is_projectable(member: NpcPoolMember) -> bool   # == not member.observation_pending
    # promoted Npc (sidequest.game.session.Npc) is always projectable

Story 75-11 is the predicate + its truth-table unit tests ONLY. Per the ADR's
implementation-story breakdown, **no wiring lands here** — consulting the predicate
from the ADR-118 ``to_card()`` / ``entity_sync`` path and the ADR-135 reference
projection is deferred to stories 75-12 and 75-13. So there is intentionally no
production consumer of this predicate yet; the reachability assertion at this tier
is that it is importable beside the model (§D3: "It lives beside the model in
``npc_pool.py``").

Design constraint (lang-review #10 — import hygiene): ``session.py`` already imports
``npc_pool.py`` (``from sidequest.game.npc_pool import NpcPoolMember``). The predicate
lives in ``npc_pool.py`` yet must also accept an ``Npc``, so it must NOT top-level
``import Npc`` — that is a circular import. Duck-typing on ``observation_pending`` (an
``Npc`` has no such field) or a ``TYPE_CHECKING`` forward-ref both satisfy the
contract; these tests assert behavior, not the chosen mechanism.

RED phase: every test here fails today —
``from sidequest.game.npc_pool import is_projectable`` raises ImportError because the
predicate does not exist yet.
"""

from __future__ import annotations

import inspect

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember, is_projectable
from sidequest.game.session import Npc


def _minimal_creature_core(name: str = "Boris") -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A weathered figure.",
        personality="Stoic.",
    )


# ---------------------------------------------------------------------------
# Reachability — the predicate lives beside the model (ADR-138 §D3)
# ---------------------------------------------------------------------------


def test_is_projectable_is_importable_and_callable() -> None:
    """§D3: the predicate "lives beside the model in ``npc_pool.py``". The import
    at module top must succeed (no circular import) and resolve to a callable.

    This is the reachability assertion appropriate to this tier — 75-11 ships the
    predicate with no production consumer (wiring is 75-12/75-13), so there is no
    production code path to drive yet."""
    assert callable(is_projectable)


# ---------------------------------------------------------------------------
# Truth table (ADR-138 §D3 / Story 75-11)
# ---------------------------------------------------------------------------


def test_ratified_pool_member_is_projectable() -> None:
    """A ratified member (``observation_pending = False``) — the world has
    committed to it — is projectable."""
    member = NpcPoolMember(
        name="Marya", drawn_from="world_authored", observation_pending=False
    )
    assert is_projectable(member) is True


def test_pending_pool_member_is_not_projectable() -> None:
    """An unratified, auto-minted phantom (``observation_pending = True``) is the
    precise case the gate exists to withhold — NOT projectable."""
    member = NpcPoolMember(
        name="Fen", drawn_from="narrator_invented", observation_pending=True
    )
    assert is_projectable(member) is False


def test_default_pool_member_is_projectable() -> None:
    """``observation_pending`` defaults ``False``: world-authored, name-generator,
    and legacy members enter the pool already ratified, so a default-constructed
    member is projectable."""
    member = NpcPoolMember(name="Wren", drawn_from="name_generator")
    assert is_projectable(member) is True


def test_promoted_npc_is_always_projectable() -> None:
    """A promoted ``Npc`` (the mechanical entity) is always projectable —
    promotion is itself the world's commitment."""
    npc = Npc(core=_minimal_creature_core())
    assert is_projectable(npc) is True


def test_promoted_npc_projectable_regardless_of_pool_origin() -> None:
    """"Always" means independent of the Npc's own state. An Npc promoted from a
    pool member (``pool_origin`` set) is still projectable — the predicate does
    not re-derive eligibility from the originating member."""
    npc = Npc(core=_minimal_creature_core(name="Marya"), pool_origin="Marya")
    assert is_projectable(npc) is True


@pytest.mark.parametrize("pending", [False, True])
def test_predicate_is_exactly_not_observation_pending(pending: bool) -> None:
    """§D3 identity: for a pool member, ``is_projectable`` is exactly
    ``not member.observation_pending`` — both branches, no other field consulted."""
    member = NpcPoolMember(
        name="Boris", drawn_from="legacy_registry", observation_pending=pending
    )
    assert is_projectable(member) == (not pending)


# ---------------------------------------------------------------------------
# Rule enforcement — lang-review #3 (type annotations at boundaries)
# ---------------------------------------------------------------------------


def test_is_projectable_has_boundary_type_annotations() -> None:
    """lang-review #3: a public predicate must annotate its parameter and return
    type. Asserted via ``inspect.signature`` (reads ``__annotations__`` as written,
    so a ``TYPE_CHECKING`` forward-ref for ``Npc`` is fine and is NOT forced to
    resolve at runtime — that would reintroduce the circular import)."""
    sig = inspect.signature(is_projectable)
    params = list(sig.parameters.values())
    assert len(params) == 1, "predicate takes exactly one argument (the entity)"
    assert params[0].annotation is not inspect.Parameter.empty, (
        "the entity parameter must be annotated"
    )
    assert sig.return_annotation in (bool, "bool"), (
        "the predicate must declare a bool return type"
    )
