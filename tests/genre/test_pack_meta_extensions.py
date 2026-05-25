"""Verify PackMeta accepts and stores an extensions list."""

from sidequest.genre.models.pack import PackMeta


def test_pack_meta_accepts_extensions_list():
    meta = PackMeta(
        name="Test Pack",
        version="1.0.0",
        description="A test genre pack",
        min_sidequest_version="0.1.0",
        extensions=["magic", "classes"],
    )
    assert meta.extensions == ["magic", "classes"]


def test_pack_meta_extensions_defaults_empty():
    meta = PackMeta(
        name="Test Pack",
        version="1.0.0",
        description="A test genre pack",
        min_sidequest_version="0.1.0",
    )
    assert meta.extensions == []
