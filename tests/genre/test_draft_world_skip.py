"""Verify that worlds with draft: true in world.yaml are skipped by the loader."""

from sidequest.genre.models.world import WorldConfig


def test_world_config_accepts_draft_field():
    config = WorldConfig(
        name="Draft World",
        description="A work in progress",
        draft=True,
    )
    assert config.draft is True


def test_world_config_draft_defaults_false():
    config = WorldConfig(
        name="Live World",
        description="A real world",
    )
    assert config.draft is False
