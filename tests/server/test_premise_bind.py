"""bind_political_state hydration (Plan 2, Task 5)."""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.premises import (
    BlocAwakening,
    BlocDef,
    PremiseClaim,
    PremiseCollapse,
    PremiseDef,
    PremiseDrain,
)
from sidequest.server.dispatch.premise_bind import bind_political_state


def _pack_with_oz():
    humbug = PremiseDef(
        premise_id="humbug",
        authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="great and terrible"),
        belief_reserve=90,
        propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose", belief_delta=40)],
        collapse=PremiseCollapse(threshold=20, outcome="He flees."),
    )
    munchkins = BlocDef(
        bloc_id="munchkins",
        defiance=5,
        grants_belief_to=["humbug"],
        awakening_acts=[BlocAwakening(act="rally", defiance_delta=10)],
        tipping_threshold=70,
        tipped_outcome="Revolt.",
    )
    world = SimpleNamespace(premises=[humbug], blocs=[munchkins])
    return SimpleNamespace(worlds={"oz": world})


def test_bind_hydrates_political_state_onto_snapshot():
    snap = GameSnapshot(world_slug="oz")
    bound = bind_political_state(_pack_with_oz(), snap, genre_slug="wry_whimsy", world_slug="oz")
    assert bound is True
    assert snap.political_state is not None
    assert snap.political_state.premises["humbug"].belief_reserve == 90
    assert snap.political_state.blocs["munchkins"].defiance == 5


def test_bind_is_noop_when_world_has_no_politics():
    pack = SimpleNamespace(worlds={"plain": SimpleNamespace(premises=[], blocs=[])})
    snap = GameSnapshot(world_slug="plain")
    bound = bind_political_state(pack, snap, genre_slug="g", world_slug="plain")
    assert bound is False
    assert snap.political_state is None


def test_bind_is_noop_for_unknown_world():
    snap = GameSnapshot(world_slug="missing")
    bound = bind_political_state(
        _pack_with_oz(), snap, genre_slug="wry_whimsy", world_slug="missing"
    )
    assert bound is False
    assert snap.political_state is None
