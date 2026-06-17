"""Story 122-8 — RED: relocate ``_KIND_TO_MESSAGE_CLS`` to the protocol tier.

ADR-147 ("Honest Layering") forbids upward imports: ``foundation <- {game,
genre, orbital, magic, interior} <- server``. The kind→message-class registry
``_KIND_TO_MESSAGE_CLS`` currently lives in ``sidequest/server/session_handler.py``
but ``sidequest/game/projection/validator.py`` reaches *up* into it via two lazy
in-method imports — the last edge the layering guard grandfathers
(``tests/infrastructure/test_import_direction_guard.py``).

This story relocates the registry down to the protocol tier (the lowest tier;
all seven message classes it maps to already live in
``sidequest/protocol/messages.py``, so the move is circular-safe), updates the
consumers to import from there, and deletes the grandfather exception so the
guard tightens to zero upward edges.

These tests pin the desired end-state and FAIL until the relocation lands.

A note on the registry's mutability (see the story session file, TEA deviation):
story-context AC1 asks for an "immutable (constructed once and frozen)"
definition, but ~13 existing test sites do
``monkeypatch.setitem(session_handler._KIND_TO_MESSAGE_CLS, ...)`` and AC4
requires the full suite to keep passing. A ``MappingProxyType`` would break all
of them. The achievable, suite-compatible contract pinned here is a single
canonical module-level dict (constructed once, one source of truth) — the
"frozen" half of AC1 is relaxed; ``test_validator_reads_the_relocated_registry``
encodes the mutation contract the rest of the suite depends on.
"""

from __future__ import annotations

from collections.abc import Mapping


def test_kind_to_message_cls_exported_from_protocol_tier() -> None:
    """AC1: the registry lives on the protocol package and preserves its mapping.

    The exact kind→class mapping must survive the move with no drift — and
    RELATIONSHIPS / LOCATION_DESCRIPTION must stay *absent* (they are emitted via
    the non-durable broadcast path, never via ``_emit_event``; registering them
    here is a latent reconnect crash per the session_handler comment).
    """
    import sidequest.protocol as protocol
    from sidequest.protocol.messages import (
        ConfrontationMessage,
        DungeonMapMessage,
        NarrationMessage,
        NarrationSegmentMessage,
        ScrapbookEntryMessage,
        SecretNoteMessage,
        TacticalGridMessage,
    )

    assert hasattr(protocol, "_KIND_TO_MESSAGE_CLS"), (
        "AC1: _KIND_TO_MESSAGE_CLS must be relocated to the protocol tier and "
        "exported from sidequest/protocol/__init__.py"
    )
    reg = protocol._KIND_TO_MESSAGE_CLS
    assert isinstance(reg, Mapping), "registry must be a mapping (kind str -> message class)"
    assert dict(reg) == {
        "NARRATION": NarrationMessage,
        "NARRATION_SEGMENT": NarrationSegmentMessage,
        "CONFRONTATION": ConfrontationMessage,
        "SECRET_NOTE": SecretNoteMessage,
        "SCRAPBOOK_ENTRY": ScrapbookEntryMessage,
        "TACTICAL_GRID": TacticalGridMessage,
        "DUNGEON_MAP": DungeonMapMessage,
    }, (
        "relocation must preserve the EXACT kind->class mapping (no drift); "
        "RELATIONSHIPS and LOCATION_DESCRIPTION must remain absent"
    )


def test_session_handler_reexports_the_same_registry_object() -> None:
    """AC2 + back-compat: session_handler re-exports the protocol object itself.

    ~10 test modules and ``server/emitters.py`` still do
    ``from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS``. After
    the move that name must resolve to the *same* relocated object — not a kept
    copy and not a fork (the SM flagged accidental fork-into-two-dicts).
    """
    import sidequest.protocol as protocol
    from sidequest.server import session_handler

    assert session_handler._KIND_TO_MESSAGE_CLS is protocol._KIND_TO_MESSAGE_CLS, (
        "AC2/back-compat: session_handler._KIND_TO_MESSAGE_CLS must BE the "
        "relocated protocol registry (identity), so existing importers keep "
        "working and there is a single source of truth"
    )


def test_validator_reads_the_relocated_registry(monkeypatch) -> None:
    """AC2/AC4 wiring: the production validator path resolves the SAME registry.

    ``validator._filter_reachable_kinds()`` derives its set from the registry at
    call time. Mutate the registry through the protocol export; if the
    production helper observes the mutation, it is reading the same object (no
    fork) AND the registry honors the mutation contract the existing
    monkeypatch.setitem suite depends on. This is the behavioral, production-path
    wiring test the project rules require — not a source-text grep.
    """
    import sidequest.protocol as protocol
    from sidequest.game.projection import validator
    from sidequest.protocol.messages import NarrationMessage

    sentinel = "TEA_RED_RELOCATION_SENTINEL"
    reg = protocol._KIND_TO_MESSAGE_CLS
    assert sentinel not in reg, "precondition: sentinel kind must not pre-exist"

    monkeypatch.setitem(reg, sentinel, NarrationMessage)

    assert sentinel in validator._filter_reachable_kinds(), (
        "AC2/AC4: validator must resolve the relocated protocol registry (same "
        "object, single source of truth). It also confirms the registry stays "
        "mutable for the monkeypatch.setitem sites across the suite."
    )


def test_grandfathered_edge_for_validator_is_removed() -> None:
    """AC3: the grandfather exception is deleted (it was the LAST upward edge)."""
    from tests.infrastructure.test_import_direction_guard import GRANDFATHERED

    assert "game/projection/validator.py" not in GRANDFATHERED, (
        "AC3: the validator.py GRANDFATHERED entry must be deleted once the upward edge is gone"
    )
    assert GRANDFATHERED == {}, (
        "AC3: validator.py was the last grandfathered upward edge; GRANDFATHERED "
        f"must be empty after relocation, got {dict(GRANDFATHERED)!r}"
    )


def test_validator_no_longer_imports_up_into_server() -> None:
    """AC2/AC3: the layering law holds with zero upward edges from validator.

    Reuses the canonical AST scanner from the layering guard rather than
    re-implementing it (catches lazy in-method imports too).
    """
    from tests.infrastructure.test_import_direction_guard import _all_server_imports

    offenders = _all_server_imports()
    assert "game/projection/validator.py" not in offenders, (
        "AC2/AC3: validator.py must import the registry from the protocol tier, "
        "not up from sidequest.server. Remaining upward edges: "
        f"{offenders.get('game/projection/validator.py')}"
    )


def test_protocol_tier_does_not_import_up_into_server() -> None:
    """Regression guard (green today): the relocation target stays layer-honest.

    protocol/ is the lowest tier and is NOT in the layering guard's
    GUARDED_TIERS, so an accidental upward import introduced *while relocating
    the registry* would slip past it. This closes that gap and enforces the
    story's "no circular imports — protocol must remain import-safe" constraint.
    Uses the guard's AST scanner, so docstring ``:func:`sidequest.server...```
    references in protocol/messages.py are correctly ignored (not imports).
    """
    from tests.infrastructure.test_import_direction_guard import (
        SIDEQUEST_PKG,
        _package_parts_for,
        _parse_module,
        _server_import_targets,
    )

    protocol_dir = SIDEQUEST_PKG / "protocol"
    offenders: dict[str, list[str]] = {}
    for path in sorted(protocol_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        targets = _server_import_targets(_parse_module(path), _package_parts_for(path))
        if targets:
            offenders[path.relative_to(SIDEQUEST_PKG).as_posix()] = sorted(targets)

    assert offenders == {}, f"protocol tier must stay clean of upward server imports: {offenders}"
