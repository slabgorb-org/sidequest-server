"""GenrePack carries an optional MutationCatalog; absence = no mutation system."""

from __future__ import annotations

from sidequest.genre.models.pack import GenrePack
from sidequest.mutation.models import MutationCatalog


def test_genre_pack_has_mutations_field() -> None:
    assert "mutations" in GenrePack.model_fields
    field = GenrePack.model_fields["mutations"]
    assert field.default is None
    # The annotation is MutationCatalog | None
    assert MutationCatalog in getattr(field.annotation, "__args__", (field.annotation,))
