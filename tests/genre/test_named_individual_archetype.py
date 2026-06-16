"""Named-individual archetypes must not be drawn into the random spawn pool.

Playtest finding (the_real_mccoy, 2026-06-01): a combat encounter rendered
"charles taze russell, Jewish". Charles Taze Russell is a *specific historical
person* authored as an NpcArchetype for flavor, but encountergen picked him via
``rng.choice(archetypes)`` and paired his name (lowercased into a combat role)
with a random culture. Specific real people should never be spawned as random
walk-ons / enemies — only deliberately, by explicit ``--archetype`` request.

Fix: an opt-in ``named_individual`` flag on NpcArchetype + a
``spawnable_archetypes`` filter applied at every random ``rng.choice`` site
(encountergen, namegen). Explicit lookups still see the full list.
"""

from __future__ import annotations

from sidequest.genre.models.character import NpcArchetype, spawnable_archetypes


def _arch(name: str, **kw: object) -> NpcArchetype:
    return NpcArchetype(name=name, description=f"{name} template", **kw)


def test_named_individual_defaults_false() -> None:
    """Archetypes are spawnable by default — the flag is opt-in."""
    assert _arch("Drifter").named_individual is False


def test_named_individual_is_settable() -> None:
    assert _arch("Charles Taze Russell", named_individual=True).named_individual is True


def test_spawnable_excludes_named_individuals() -> None:
    pool = [
        _arch("Drifter"),
        _arch("Charles Taze Russell", named_individual=True),
        _arch("Mill Hand"),
        _arch("Abner Doubleday", named_individual=True),
    ]

    spawnable = spawnable_archetypes(pool)

    assert [a.name for a in spawnable] == ["Drifter", "Mill Hand"]


def test_spawnable_empty_when_all_named() -> None:
    """Caller can detect the all-named case and fail loud (no silent spawn)."""
    pool = [_arch("Russell", named_individual=True), _arch("Keely", named_individual=True)]

    assert spawnable_archetypes(pool) == []
