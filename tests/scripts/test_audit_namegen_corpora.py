"""Tests for ``scripts/audit_namegen_corpora.py`` (Story 45-28).

The audit script is the wire-first treatment for AC1 (corpus size
audit) — it must hit the actual culture-loading path
(``sidequest.genre.load_genre_pack``), resolve every culture's
``corpora`` references to disk paths, count words, and produce a
markdown report with three sections:

- **OK** — corpus ≥ ``WARN_BELOW_WORDS``
- **THIN** — ``FAIL_BELOW_WORDS`` ≤ corpus < ``WARN_BELOW_WORDS``
- **FAIL** — corpus < ``FAIL_BELOW_WORDS``

Exit code:

- ``0`` if no FAIL rows (THIN allowed — those are warnings, not gates)
- ``1`` if any FAIL row (CI gate signal)
- ``2`` reserved for invocation errors (missing pack, bad ``--path``)

The architect context (``Audit script — wire-first applied to content``)
specifies modeling on ``audit_content_drift.py``. Tests pin the
contract: shape of the output, exit code semantics, and the
content-state regression (post-expansion no THIN rows for the three
named corpora).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_ROOT = REPO_ROOT / "sidequest-server"
SCRIPT = SERVER_ROOT / "scripts" / "audit_namegen_corpora.py"
CONTENT_ROOT = REPO_ROOT / "sidequest-content"


# ---------------------------------------------------------------------------
# Script existence
# ---------------------------------------------------------------------------


def test_audit_script_exists() -> None:
    """``scripts/audit_namegen_corpora.py`` lives at the architect-specified path.

    The script is the AC1 deliverable; its path is part of the contract
    so CI hooks (``just check`` extensions, pre-commit) can find it.
    """
    assert SCRIPT.is_file(), (
        f"audit script must live at {SCRIPT.relative_to(REPO_ROOT)}; "
        "see context-story-45-28.md 'Audit script — wire-first applied "
        "to content'."
    )


# ---------------------------------------------------------------------------
# Live tree — runs against the real sidequest-content
# ---------------------------------------------------------------------------


def _run_audit(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(SERVER_ROOT),
    )


def test_audit_live_tree_exits_zero_after_corpus_expansion() -> None:
    """After the AC3 corpus expansion lands, no FAIL rows on the live tree.

    Today (pre-fix) this test fails because latin/polynesian/georgian
    are all THIN — but THIN is exit 0, not exit 1. The exit-code
    contract is: FAIL only blocks CI. So this test passes pre-fix as
    long as no corpus is below FAIL_BELOW_WORDS=200, which is true.

    The post-fix value of this test: it gates against a regression
    where someone replaces a corpus with a stub. Pin the contract now.
    """
    result = _run_audit()
    assert result.returncode == 0, (
        f"live audit on the real content tree must not produce FAIL rows. "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_audit_live_tree_reports_named_thin_corpora_resolved() -> None:
    """The audit walks cultures, not just files — corpora must appear under cultures.

    Without the wire-first treatment, an audit could just ``ls
    corpus/*.txt`` and miss that latin.txt is consumed by Span
    Aristocracy. The whole point of the audit is to surface
    consumption-by-culture so a "fix the latin corpus" task knows
    *which culture suffers*.

    Pre-fix, all three named corpora appear; post-fix (corpus
    expansion), they appear under their consuming cultures with OK
    status. Either way, they show up.

    Note: Span Aristocracy / Vaal-Kesh / Makhani live in
    ``genre_workshopping/space_opera/worlds/aureate_span/`` (relocated
    from ``genre_packs/`` in commit acc89a3 — see the workshopping skip
    comment in tests/game/test_wire_genre_resources.py). The audit's
    default ``--path`` is ``genre_packs/``; we run it twice — once on
    each root — and combine the output so both production-canonical and
    in-development cultures get coverage. When the aureate_span pack
    graduates back to genre_packs, the second invocation becomes
    redundant but the assertion still passes.
    """
    workshopping_path = REPO_ROOT / "sidequest-content" / "genre_workshopping"

    result_packs = _run_audit()
    result_workshopping = _run_audit("--path", str(workshopping_path))
    out = result_packs.stdout + "\n" + result_workshopping.stdout

    # Every named-thin corpus shows up in the report — pre-fix as THIN,
    # post-fix as OK. We don't assert status here (that's the
    # corpus-shape regression test below).
    assert "latin.txt" in out
    assert "polynesian.txt" in out
    assert "georgian.txt" in out

    # Cultures consuming them are named so the report is actionable.
    assert "Span Aristocracy" in out
    assert "Vaal-Kesh" in out
    assert "Makhani" in out


def test_audit_live_tree_no_named_corpora_left_thin_post_expansion() -> None:
    """AC3 regression: post-expansion, the three Aureate Span corpora are OK.

    Pre-fix this test fails — those three corpora are in the THIN band
    today (309-340 words). It's the most direct shape-of-content
    regression test we can write: if Dev expands them past
    ``WARN_BELOW_WORDS`` (1000), this passes; if a future commit
    truncates them back below 1000, this fails.
    """
    result = _run_audit()
    assert result.returncode == 0, (
        f"audit invocation failed (rc={result.returncode}); cannot judge "
        f"corpus markers from an unsuccessful run. stderr:\n{result.stderr}"
    )
    out = result.stdout
    assert out.strip(), (
        "audit produced empty stdout — cannot assert markers on a report that doesn't exist"
    )

    # The audit script's output marks each corpus row with OK/THIN/FAIL.
    # We don't pin the exact format string (Dev picks it), but we DO
    # require that none of the three named corpora carry a THIN or FAIL
    # marker.
    for corpus_name in ("latin.txt", "polynesian.txt", "georgian.txt"):
        # Each named corpus must appear at least once in the report —
        # the audit walks cultures so consumed corpora are always listed.
        assert corpus_name in out, (
            f"{corpus_name} missing from audit report — report shape "
            f"may have regressed. stdout:\n{out}"
        )
        for status in ("THIN", "FAIL"):
            for line in out.splitlines():
                if corpus_name in line and status in line:
                    pytest.fail(
                        f"{corpus_name} still flagged {status} after expansion; line: {line!r}"
                    )


def test_audit_live_tree_corpora_above_warn_threshold() -> None:
    """Direct word-count regression on the three expanded corpora.

    Belt-and-braces alongside the audit-marker test above: read the
    files directly and assert ``len(text.split()) >= WARN_BELOW_WORDS``.
    If the audit script's marker logic ever drifts, this test still
    catches a corpus shrinkage.
    """
    from sidequest.genre.names.thresholds import WARN_BELOW_WORDS

    corpus_dir = CONTENT_ROOT / "genre_packs" / "space_opera" / "corpus"
    for corpus_name in ("latin.txt", "polynesian.txt", "georgian.txt"):
        path = corpus_dir / corpus_name
        assert path.is_file(), f"missing corpus {path}"
        word_count = len(path.read_text(encoding="utf-8").split())
        assert word_count >= WARN_BELOW_WORDS, (
            f"{corpus_name} has {word_count} words; AC3 requires "
            f">= {WARN_BELOW_WORDS} (post-expansion floor)."
        )


# ---------------------------------------------------------------------------
# Synthetic fixture — exit-code semantics under controlled input
# ---------------------------------------------------------------------------


def _build_synthetic_pack(root: Path, *, corpus_word_count: int) -> Path:
    """Build a minimal genre pack at ``root/genre_packs/synth/``.

    The pack ships exactly one culture pointing at one corpus file
    sized to the given ``corpus_word_count``. Used to exercise both
    the FAIL exit-code path (sub-200 word file) and the OK exit-code
    path (above 1000) in a single fixture.
    """
    pack_dir = root / "genre_packs" / "synth"
    pack_dir.mkdir(parents=True)
    (pack_dir / "corpus").mkdir()
    (pack_dir / "names").mkdir()

    corpus_path = pack_dir / "corpus" / "synth.txt"
    words = " ".join(f"word{i}" for i in range(corpus_word_count))
    corpus_path.write_text(words, encoding="utf-8")

    # Minimal pack files — only what load_genre_pack requires for the
    # culture lookup path. The audit script consults Culture's slot
    # ``corpora`` references, so anything else can be a stub.
    (pack_dir / "pack.yaml").write_text(
        "id: synth\nname: synth\ndescription: synth test pack\n",
        encoding="utf-8",
    )
    (pack_dir / "cultures.yaml").write_text(
        """\
- name: Synth Culture
  summary: synthetic test culture
  description: synthetic test culture
  slots:
    given_name:
      corpora:
        - corpus: synth.txt
          weight: 1.0
      lookback: 2
  person_patterns:
    - "{given_name}"
""",
        encoding="utf-8",
    )
    return pack_dir


def test_audit_synthetic_fail_corpus_exits_one(tmp_path: Path) -> None:
    """A 50-word corpus → exit code 1 + FAIL row in the report.

    This is the negative test the architect context calls out: "invoke
    against a fixture pack with a 50-word synthetic corpus. Assert
    exit code 1 and a FAIL row." Without it, a regression where exit
    codes flatten (every status returns 0) passes silently.
    """
    _build_synthetic_pack(tmp_path, corpus_word_count=50)

    result = _run_audit("--path", str(tmp_path / "genre_packs"))

    assert result.returncode == 1, (
        f"50-word corpus must trigger exit 1; got {result.returncode}. "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    out = result.stdout
    assert "FAIL" in out, "report must label sub-FAIL corpora"
    assert "synth.txt" in out


def test_audit_synthetic_ample_corpus_exits_zero(tmp_path: Path) -> None:
    """A 1500-word corpus → exit code 0 + OK row in the report."""
    _build_synthetic_pack(tmp_path, corpus_word_count=1500)

    result = _run_audit("--path", str(tmp_path / "genre_packs"))

    assert result.returncode == 0
    assert "synth.txt" in result.stdout


def test_audit_synthetic_thin_corpus_exits_zero_with_thin_marker(
    tmp_path: Path,
) -> None:
    """A 300-word corpus → exit code 0 (THIN is a warning, not a gate) + THIN marker.

    THIN must surface visually in the report so an operator can act on
    it; but it must NOT block CI — that's reserved for FAIL.
    """
    _build_synthetic_pack(tmp_path, corpus_word_count=300)

    result = _run_audit("--path", str(tmp_path / "genre_packs"))

    assert result.returncode == 0, (
        f"THIN corpus is a warning, not a CI gate; expected exit 0, got {result.returncode}"
    )
    assert "THIN" in result.stdout
    assert "synth.txt" in result.stdout
