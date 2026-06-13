from sidequest.game.resource_wiring import wire_genre_resources
from sidequest.game.session import GameSnapshot


class _FakeRules:
    def __init__(self, resources):
        self.resources = resources


class _FakePack:
    def __init__(self, resources):
        self.rules = _FakeRules(resources)


def _decl(name, starting, mx):
    # ResourceDeclaration is a pydantic model; build via the real type.
    from sidequest.genre.models.rules import ResourceDeclaration

    return ResourceDeclaration(
        name=name,
        label=name.title(),
        min=0.0,
        max=mx,
        starting=starting,
        voluntary=False,
        decay_per_turn=0.0,
    )


def test_wire_populates_declared_pools():
    snap = GameSnapshot()
    pack = _FakePack([_decl("light", 0.0, 6.0)])
    wire_genre_resources(snap, pack)
    assert "light" in snap.resources
    assert snap.resources["light"].current == 0.0
    assert snap.resources["light"].max == 6.0


def test_wire_is_noop_without_pack_or_rules():
    snap = GameSnapshot()
    wire_genre_resources(snap, None)  # no pack
    wire_genre_resources(snap, _FakePack([]))  # empty declarations
    assert snap.resources == {}


def test_wire_preserves_existing_current_on_upsert():
    snap = GameSnapshot()
    pack = _FakePack([_decl("light", 6.0, 6.0)])
    wire_genre_resources(snap, pack)
    snap.resources["light"].current = 2.0  # mid-delve burn
    wire_genre_resources(snap, pack)  # reload
    assert snap.resources["light"].current == 2.0  # preserved, not reset to starting
