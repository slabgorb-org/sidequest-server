"""mutations.yaml -> MutationCatalog. Fail-loud; absence is the CALLER's
decision (the genre loader treats a missing file as 'pack has no mutation
system' — a deliberate authoring choice, mirroring magic.yaml handling)."""

from __future__ import annotations

from pathlib import Path

import yaml

from sidequest.mutation.models import MutationCatalog


def load_mutation_catalog(path: Path) -> MutationCatalog:
    if not path.is_file():
        raise FileNotFoundError(f"mutation catalog not found: {path} (expected mutations.yaml)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return MutationCatalog.model_validate(raw)
