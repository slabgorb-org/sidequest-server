"""ADR-153 §2 firewall — pack-validator guard (Task 5).

Per the project rule ``feedback_no_content_in_unit_tests``, the live pack's
dogfight-def shape is a content invariant — it belongs in the pack validator,
not pytest. This guards the 158-31 contradiction (``sealed_letter_lookup`` +
``win_condition: dial_threshold``) so it can never reappear in content: any
``sealed_letter_lookup`` *combat* confrontation must resolve via
``hp_depletion``.

RED today: ``sidequest.cli.validate.rules`` does not exist yet (ImportError),
and the ``rules`` subcommand is not registered in the validate CLI group.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def _write_rules(pack_dir: Path, *, win_condition: str) -> Path:
    """Write a minimal ``rules.yaml`` with a single sealed-letter combat
    confrontation at the given ``win_condition``. The rules validator reads
    ``rules.yaml`` raw (the same way ``validate.audio._check_rules_moods`` does),
    so only the keys the firewall check inspects need be present."""
    (pack_dir / "rules.yaml").write_text(
        textwrap.dedent(
            f"""\
            confrontations:
              - type: dogfight
                label: Fighter Duel
                category: combat
                resolution_mode: sealed_letter_lookup
                win_condition: {win_condition}
            """
        ),
        encoding="utf-8",
    )
    return pack_dir


def test_validator_rejects_sealed_letter_combat_without_hp_depletion(
    tmp_path: Path,
) -> None:
    """A sealed-letter *combat* def that does not resolve via hp_depletion is an
    error (the 158-31 contradiction)."""
    from sidequest.cli.validate.rules import validate_rules_in_pack

    pack_dir = _write_rules(tmp_path, win_condition="dial_threshold")
    result = validate_rules_in_pack(pack_dir)

    assert not result.success, (
        "a sealed_letter_lookup combat def with win_condition=dial_threshold must "
        "be a validation error (ADR-153 firewall)"
    )
    messages = [issue.message for issue in result.errors]
    assert any("sealed_letter" in m and "hp_depletion" in m for m in messages), (
        f"expected an error naming sealed_letter + hp_depletion, got {messages!r}"
    )


def test_validator_accepts_sealed_letter_combat_with_hp_depletion(
    tmp_path: Path,
) -> None:
    """The compliant shape (hp_depletion) produces no firewall error."""
    from sidequest.cli.validate.rules import validate_rules_in_pack

    pack_dir = _write_rules(tmp_path, win_condition="hp_depletion")
    result = validate_rules_in_pack(pack_dir)

    messages = [issue.message for issue in result.errors]
    assert not any("sealed_letter" in m and "hp_depletion" in m for m in messages), (
        f"a compliant hp_depletion def must not be flagged; got errors {messages!r}"
    )


def test_rules_validator_registered_in_cli() -> None:
    """Wiring: the new ``rules`` validator must be reachable from the validate
    CLI group, not just importable in isolation (CLAUDE.md: every test suite
    needs a wiring test). This interrogates the click registry, not source text."""
    from sidequest.cli.validate.__main__ import cli

    assert "rules" in cli.commands, (
        "the 'rules' validator must be registered as a subcommand of the "
        "`python -m sidequest.cli.validate` group"
    )
