"""GenrePack.effective_cultures / effective_archetypes — world-over-genre
resolution (perseus_cloud Monster-Manual seeding bug, 2026-05-29).

A world that declares its own cultures REPLACES the genre set for naming and
seeding (SOUL "Crunch in the Genre, Flavor in the World" — cultural identity is
a World concern). namegen already resolved this way; Monster-Manual seeding
(``pregen.seed_manual``) read ``pack.cultures`` raw, so it handed genre culture
names to a name generator that validates against the WORLD set → every seed
failed (``Culture 'Hegemonic' not found``) → zero NPCs seeded. This pins the
single shared resolution both call sites must use.
"""

from __future__ import annotations

from sidequest.genre.models.culture import Culture
from sidequest.genre.models.pack import GenrePack, World


def _culture(name: str) -> Culture:
    return Culture.model_construct(name=name, summary="", description="")


def _pack(
    *,
    genre_cultures: list[str],
    worlds: dict[str, World] | None = None,
    genre_archetypes: list[str] | None = None,
) -> GenrePack:
    # model_construct bypasses the (many) required submodels — methods are
    # class-level so effective_cultures/effective_archetypes work regardless.
    from sidequest.genre.models.character import NpcArchetype

    return GenrePack.model_construct(
        cultures=[_culture(n) for n in genre_cultures],
        archetypes=[NpcArchetype.model_construct(name=n) for n in (genre_archetypes or [])],
        worlds=worlds or {},
    )


def _world(*, cultures: list[str] | None = None, archetypes: list[str] | None = None) -> World:
    from sidequest.genre.models.character import NpcArchetype

    return World.model_construct(
        cultures=[_culture(n) for n in (cultures or [])],
        archetypes=[NpcArchetype.model_construct(name=n) for n in (archetypes or [])],
    )


# --- cultures ---------------------------------------------------------------


def test_world_cultures_replace_genre_when_present() -> None:
    pack = _pack(
        genre_cultures=["Hegemonic", "Frontier"],
        worlds={"perseus": _world(cultures=["Spacer", "Thari", "Yulan"])},
    )
    cultures, source = pack.effective_cultures("perseus")
    assert [c.name for c in cultures] == ["Spacer", "Thari", "Yulan"]
    assert source == "world"


def test_genre_cultures_when_world_declares_none() -> None:
    pack = _pack(genre_cultures=["Hegemonic"], worlds={"perseus": _world(cultures=[])})
    cultures, source = pack.effective_cultures("perseus")
    assert [c.name for c in cultures] == ["Hegemonic"]
    assert source == "genre"


def test_genre_cultures_when_world_arg_none() -> None:
    pack = _pack(genre_cultures=["Hegemonic"])
    cultures, source = pack.effective_cultures(None)
    assert [c.name for c in cultures] == ["Hegemonic"]
    assert source == "genre"


def test_genre_cultures_when_world_unknown() -> None:
    pack = _pack(genre_cultures=["Hegemonic"], worlds={"perseus": _world(cultures=["Spacer"])})
    cultures, source = pack.effective_cultures("no_such_world")
    assert [c.name for c in cultures] == ["Hegemonic"]
    assert source == "genre"


# --- archetypes (same resolution, kept in lockstep so namegen can use it) ---


def test_world_archetypes_replace_genre_when_present() -> None:
    pack = _pack(
        genre_cultures=["Hegemonic"],
        genre_archetypes=["Soldier"],
        worlds={"perseus": _world(archetypes=["Voidrunner"])},
    )
    archetypes, source = pack.effective_archetypes("perseus")
    assert [a.name for a in archetypes] == ["Voidrunner"]
    assert source == "world"


def test_genre_archetypes_when_world_declares_none() -> None:
    pack = _pack(
        genre_cultures=["Hegemonic"],
        genre_archetypes=["Soldier"],
        worlds={"perseus": _world(archetypes=[])},
    )
    archetypes, source = pack.effective_archetypes("perseus")
    assert [a.name for a in archetypes] == ["Soldier"]
    assert source == "genre"
