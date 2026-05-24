"""CLI: walk a pack directory's YAML files and report every (file_stem, key_path)
not classified in reference_visibility.PUBLIC ∪ KEEPER.

Exit nonzero on any unclassified field so CI / pre-merge gates fail loud."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from sidequest.server.reference_visibility import (
    KEEPER,  # noqa: F401
    PUBLIC,  # noqa: F401
    Visibility,
    classify,
)

KeyPath = tuple[str, ...]


def _walk_node(node: object, key_path: KeyPath) -> list[tuple[KeyPath, object]]:
    """Yield (key_path, leaf_or_container) tuples for every reachable key."""
    out: list[tuple[KeyPath, object]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = key_path + (str(key),)
            out.append((child_path, value))
            out.extend(_walk_node(value, child_path))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                # Use "*" as the wildcard slot so the path matches a wildcard entry.
                child_path = key_path + ("*",)
                out.extend(_walk_node(item, child_path))
    return out


def scan_pack(pack_dir: Path) -> list[tuple[str, KeyPath]]:
    """Return a sorted list of (file_stem, key_path) not in PUBLIC ∪ KEEPER."""
    unclassified: set[tuple[str, KeyPath]] = set()
    for yaml_path in sorted(pack_dir.rglob("*.yaml")):
        try:
            with yaml_path.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError:
            continue
        if data is None:
            continue
        stem = yaml_path.stem
        # The whole-file entry is also classifiable.
        if classify(stem, ()) is Visibility.UNKNOWN:
            unclassified.add((stem, ()))
        for path, _value in _walk_node(data, ()):
            if classify(stem, path) is Visibility.UNKNOWN:
                unclassified.add((stem, path))
    return sorted(unclassified)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_dir", type=Path, help="Path to a genre pack directory")
    parser.add_argument(
        "--only-renderer-stems",
        action="store_true",
        help="Only report unknowns under stems the reference renderer actually reads.",
    )
    args = parser.parse_args(argv)
    unclassified = scan_pack(args.pack_dir)
    if args.only_renderer_stems:
        from sidequest.server.reference_visibility import PUBLIC_STEMS

        unclassified = [(stem, path) for (stem, path) in unclassified if stem in PUBLIC_STEMS]
    if not unclassified:
        print(f"OK — every field in {args.pack_dir} is classified.")
        return 0
    print(f"FAIL — {len(unclassified)} unclassified (file_stem, key_path) tuples:")
    for stem, path in unclassified:
        print(f"  ({stem!r}, {path!r})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
