"""Tests for ``scripts/audit_namegen_corpora.py`` (Stories 45-28, 64-7).

The audit script walks every culture's ``corpora`` references, resolves
each to a disk path — checking the pack's own ``corpus/`` first, then the
centralized ``sidequest-content/corpus/shared/`` fallback the runtime
resolver uses (``generator.py:_resolve_corpus_file``; added in Story
64-7) — counts words, and produces a markdown report with four
status bands:

- **OK** — corpus ≥ ``WARN_BELOW_WORDS``
- **THIN** — ``FAIL_BELOW_WORDS`` ≤ corpus < ``WARN_BELOW_WORDS``
- **FAIL** — corpus < ``FAIL_BELOW_WORDS``
- **MISSING** — corpus found in neither the pack ``corpus/`` nor the
  shared fallback

Exit code:

- ``0`` if no FAIL and no MISSING rows (THIN allowed — warnings, not gates)
- ``1`` if any FAIL or MISSING row (CI gate signal)
- ``2`` reserved for invocation errors (missing pack, bad ``--path``)

Tests pin the contract: shape of the output, exit-code semantics, the
shared-fallback resolution parity with the runtime resolver (Story 64-7),
and the No-Silent-Fallbacks invariant (a genuinely absent corpus still
reports MISSING / rc=1).
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


def test_audit_surfaces_consumption_by_culture() -> None:
    """The audit walks cultures, not just files — corpora appear under their consumers.

    Story 64-7: the audit's value is surfacing WHICH culture consumes a
    given corpus, so a "fix corpus X" task knows which culture suffers.
    The ``space_opera`` cultures consume the shared-resolved trio:

    - Hegemonic  -> latin.txt
    - Voidborn   -> polynesian.txt
    - Tsveri     -> georgian.txt

    All three corpora live ONLY in ``sidequest-content/corpus/shared/``
    (no per-pack copy), so the audit can only resolve them via the same
    shared fallback the runtime resolver uses
    (``generator.py:_resolve_corpus_file``).

    These resolve (OK, not MISSING) and are attributed to the consuming
    culture so the report stays actionable.

    Tier note: epic-74 moved ``space_opera``'s flavor (cultures) out of the
    genre tier and into the ``coyote_star`` world — the genre-level
    ``cultures.yaml`` was deleted and these cultures now live in
    ``worlds/coyote_star/cultures/*.yaml`` (the per-culture-directory
    model). The audit walks that directory model since Story 64-15, so the
    rows are tagged ``(world:coyote_star)``. The substring assertions below
    are tier-agnostic — they match the culture name and corpus on the same
    row regardless of whether the culture is genre- or world-tier.
    """
    out = _run_audit().stdout
    assert out.strip(), "audit produced empty stdout — cannot assert on an absent report"

    for culture, corpus_name in (
        ("Hegemonic", "latin.txt"),
        ("Voidborn", "polynesian.txt"),
        ("Tsveri", "georgian.txt"),
    ):
        # The corpus must be attributed to its consuming culture on the SAME
        # report row (the audit walks cultures, so each corpus is listed under
        # its consumer). Asserting co-location — not two independent substrings —
        # guards against mis-attribution and against either name leaking in from
        # an unrelated row or an error trace.
        rows = [line for line in out.splitlines() if culture in line and corpus_name in line]
        assert rows, (
            f"{corpus_name} not attributed to consuming culture {culture!r} on any "
            f"report row — the audit must surface consumption-by-culture to stay "
            f"actionable.\nstdout:\n{out}"
        )
        # Post-fix the corpus resolves via corpus/shared/, so its row(s) must NOT
        # be flagged MISSING.
        for line in rows:
            assert "MISSING" not in line, (
                f"{corpus_name} still MISSING after the shared-fallback fix; the "
                f"audit is not resolving corpus/shared/. line: {line!r}"
            )


def test_audit_live_tree_no_named_corpora_left_thin_post_expansion() -> None:
    """Regression: the three named corpora resolve OK (not THIN/FAIL) on the live tree.

    Pre-fix this test fails because the audit exits rc=1 (the trio show
    as MISSING — the audit can't find them without the corpus/shared
    fallback). Post-fix the trio resolve from corpus/shared/ with
    count_words latin=1326 / polynesian=1005 / georgian=1004 — all just
    clear ``WARN_BELOW_WORDS`` (1000), so they carry no THIN/FAIL marker.
    If a future commit truncates any of them below 1000, this fails.
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


def test_shared_corpora_clear_warn_threshold() -> None:
    """Direct word-count regression on the three named corpora — at their REAL home.

    Story 64-7 correction: these corpora were never per-pack files. They
    live in the centralized ``sidequest-content/corpus/shared/`` that the
    runtime resolver (``generator.py:_resolve_corpus_file``) and, post-fix,
    the audit both fall back to. The prior version of this test read
    ``genre_packs/space_opera/corpus/latin.txt`` — a path that never
    existed — and so failed for the wrong reason.

    Belt-and-braces alongside the consumption-by-culture test above: read
    the files where they actually live and assert they clear WARN using
    the SAME ``count_words`` the audit uses, so this regression guard
    moves in lockstep with the audit's own classification. Catches a
    shared-corpus shrinkage even if the audit's marker logic drifts.
    """
    from sidequest.genre.names.thresholds import WARN_BELOW_WORDS, count_words

    shared_dir = CONTENT_ROOT / "corpus" / "shared"
    for corpus_name in ("latin.txt", "polynesian.txt", "georgian.txt"):
        path = shared_dir / corpus_name
        assert path.is_file(), (
            f"{corpus_name} must live in corpus/shared/ (single source of "
            f"truth per ADR-091); missing at {path}"
        )
        word_count = count_words(path.read_text(encoding="utf-8"))
        assert word_count >= WARN_BELOW_WORDS, (
            f"{corpus_name} has {word_count} words (count_words); below the "
            f"WARN floor of {WARN_BELOW_WORDS} the audit will flag it THIN."
        )


def test_audit_live_tree_reports_zero_missing() -> None:
    """Story 64-7 core AC: no corpus the audit walks is MISSING on the live tree.

    Every corpus referenced by a walked culture must resolve — either in
    the pack's own ``corpus/`` dir or via the ``corpus/shared/`` fallback
    the runtime resolver already uses. Pre-fix: 61 MISSING rows, audit
    exits rc=1. Post-fix: 0 MISSING, rc=0. This is the regression that
    proves the audit stopped reporting MISSING for files that resolve
    fine at runtime.

    Asserting on the ``## MISSING`` section header (rendered only when
    MISSING rows exist) rather than the bare substring ``MISSING`` —
    the summary line always prints ``... 0 MISSING ...`` post-fix.
    """
    result = _run_audit()
    out = result.stdout
    assert "## MISSING" not in out, (
        "audit still reports MISSING corpora that resolve at runtime via "
        f"corpus/shared/.\nstdout:\n{out}"
    )
    assert "0 MISSING" in out, f"audit summary should report 0 MISSING post-fix.\nstdout:\n{out}"
    assert result.returncode == 0, (
        f"audit must exit 0 once shared corpora resolve; got "
        f"{result.returncode}.\nstdout:\n{out}\nstderr:\n{result.stderr}"
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


# ---------------------------------------------------------------------------
# Shared-fallback parity (Story 64-7) — the audit must resolve a corpus
# from corpus/shared/ exactly as the runtime resolver does, and must NOT
# paper over a corpus that is genuinely absent everywhere.
# ---------------------------------------------------------------------------


def _build_synthetic_pack_shared_only(root: Path, *, corpus_word_count: int) -> Path:
    """Build a pack whose corpus lives ONLY in ``root/corpus/shared/``.

    Mirrors the production layout that broke the audit: a culture
    references ``shared_only.txt`` but the pack has NO per-pack
    ``corpus/`` copy. The file sits in the centralized
    ``corpus/shared/`` that the runtime resolver falls back to
    (``generator.py:_resolve_corpus_file``, fed
    ``pack.source_dir.parent.parent / "corpus" / "shared"`` by
    ``narration_apply.py`` and the namegen/encountergen CLIs).

    The audit computes the same fallback from the pack dir:
    ``pack_dir.parent.parent / "corpus" / "shared"`` →
    ``root/corpus/shared``. Pre-fix the audit ignores it and reports
    MISSING; post-fix it resolves.
    """
    pack_dir = root / "genre_packs" / "synthshared"
    pack_dir.mkdir(parents=True)
    (pack_dir / "names").mkdir()
    # Deliberately NO ``pack_dir / "corpus"`` — force the shared fallback.

    shared_dir = root / "corpus" / "shared"
    shared_dir.mkdir(parents=True)
    words = " ".join(f"word{i}" for i in range(corpus_word_count))
    (shared_dir / "shared_only.txt").write_text(words, encoding="utf-8")

    (pack_dir / "pack.yaml").write_text(
        "id: synthshared\nname: synthshared\ndescription: shared-fallback test pack\n",
        encoding="utf-8",
    )
    (pack_dir / "cultures.yaml").write_text(
        """\
- name: Shared Culture
  summary: shared-fallback test culture
  description: shared-fallback test culture
  slots:
    given_name:
      corpora:
        - corpus: shared_only.txt
          weight: 1.0
      lookback: 2
  person_patterns:
    - "{given_name}"
""",
        encoding="utf-8",
    )
    return pack_dir


def test_audit_synthetic_shared_fallback_resolves(tmp_path: Path) -> None:
    """A corpus present ONLY in corpus/shared/ resolves — parity with the runtime resolver.

    This is the heart of Story 64-7. The runtime resolver
    (``generator.py:_resolve_corpus_file``) finds a corpus via the
    ``corpus/shared/`` fallback when the pack ships no per-pack copy; the
    audit must agree, or it reports false MISSING for files that resolve
    fine at runtime.

    Pre-fix: the audit only checks ``pack_dir/corpus/`` → MISSING → rc=1
    (this assertion fails). Post-fix: resolves via the shared fallback →
    OK → rc=0.
    """
    _build_synthetic_pack_shared_only(tmp_path, corpus_word_count=1500)

    result = _run_audit("--path", str(tmp_path / "genre_packs"))

    assert result.returncode == 0, (
        f"a corpus resolvable via corpus/shared/ must not be MISSING "
        f"(rc=0 expected); got {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "shared_only.txt" in result.stdout
    assert "## MISSING" not in result.stdout, (
        "a shared-resolved corpus must NOT be flagged MISSING — the audit "
        f"must mirror the runtime fallback.\nstdout:\n{result.stdout}"
    )
    # Confirm it landed as a real, counted OK row: the corpus name and the
    # OK status must appear on the SAME report row. A bare ``"OK" in stdout``
    # would be vacuous — the summary line always prints "... N OK." even when
    # zero corpora resolved.
    assert any("shared_only.txt" in line and "OK" in line for line in result.stdout.splitlines()), (
        "shared_only.txt did not land as a counted OK row — the audit must "
        f"resolve AND classify it, not merely mention it.\nstdout:\n{result.stdout}"
    )


def test_audit_synthetic_absent_corpus_still_missing(tmp_path: Path) -> None:
    """A corpus in NEITHER the pack nor corpus/shared/ stays MISSING (rc=1).

    No Silent Fallbacks: the shared fallback must resolve real files, not
    suppress the MISSING signal for a corpus that is genuinely absent.
    This pins that the fix narrows the gap to *resolvable* files and does
    not blanket-silence MISSING. Passes both pre- and post-fix; it guards
    the fix from over-reaching.
    """
    pack_dir = tmp_path / "genre_packs" / "synthabsent"
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        "id: synthabsent\nname: synthabsent\ndescription: absent-corpus test pack\n",
        encoding="utf-8",
    )
    (pack_dir / "cultures.yaml").write_text(
        """\
- name: Absent Culture
  summary: absent-corpus test culture
  description: absent-corpus test culture
  slots:
    given_name:
      corpora:
        - corpus: does_not_exist.txt
          weight: 1.0
      lookback: 2
  person_patterns:
    - "{given_name}"
""",
        encoding="utf-8",
    )

    result = _run_audit("--path", str(tmp_path / "genre_packs"))

    assert result.returncode == 1, (
        f"a genuinely-absent corpus must still exit 1; got {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "does_not_exist.txt" in result.stdout
    assert "## MISSING" in result.stdout


# ---------------------------------------------------------------------------
# Per-culture-directory world discovery (Story 64-15)
#
# The audit's ``_audit_pack`` walks world cultures via the SINGLE-FILE model
# only — ``worlds/<world>/cultures.yaml``. But the loader
# (``sidequest/genre/loader.py``, ~L895-914) discovers world cultures from
# EITHER a single ``cultures.yaml`` OR a per-culture ``cultures/*.yaml``
# DIRECTORY, with the directory taking precedence when both exist. Live
# worlds ``space_opera/aureate_span`` and ``space_opera/perseus_cloud``
# (incl. the ``yulan`` corpus) use the directory model, so the audit is
# blind to them. This block pins the post-fix behavior: the audit must
# mirror the loader's discovery — directory model, precedence, and the
# loader's skip rules (``.gitkeep`` and no-``name`` art overlays).
#
# Found by TEA/Reviewer during 64-7 (see the note in
# ``test_audit_surfaces_consumption_by_culture`` above).
# ---------------------------------------------------------------------------

SPACE_OPERA = CONTENT_ROOT / "genre_packs" / "space_opera"


def _world_dir_culture_yaml(name: str, corpus_filename: str) -> str:
    """One per-culture-dir file: a SINGLE Culture dict (not a list).

    Matches the on-disk shape of e.g.
    ``space_opera/worlds/perseus_cloud/cultures/yulan.yaml`` — one mapping
    per file, in contrast to the genre-tier ``cultures.yaml`` which is a
    list. The ``{given_name}`` in the pattern is an escaped literal brace.
    """
    return (
        f"name: {name}\n"
        f"summary: per-culture-dir test culture\n"
        f"description: per-culture-dir test culture\n"
        f"slots:\n"
        f"  given_name:\n"
        f"    corpora:\n"
        f"      - corpus: {corpus_filename}\n"
        f"        weight: 1.0\n"
        f"    lookback: 2\n"
        f"person_patterns:\n"
        f"  - '{{given_name}}'\n"
    )


def _genre_culture_list_yaml(name: str, corpus_filename: str) -> str:
    """A genre-tier ``cultures.yaml`` body: a LIST of one Culture mapping."""
    return (
        f"- name: {name}\n"
        f"  summary: genre-tier test culture\n"
        f"  description: genre-tier test culture\n"
        f"  slots:\n"
        f"    given_name:\n"
        f"      corpora:\n"
        f"        - corpus: {corpus_filename}\n"
        f"          weight: 1.0\n"
        f"      lookback: 2\n"
        f"  person_patterns:\n"
        f"    - '{{given_name}}'\n"
    )


def _corpus_words(n: int) -> str:
    return " ".join(f"word{i}" for i in range(n))


def _scaffold_pack(root: Path, pack_name: str) -> Path:
    """Create ``root/genre_packs/<pack_name>/`` with a ``corpus/`` dir + pack.yaml."""
    pack_dir = root / "genre_packs" / pack_name
    (pack_dir / "corpus").mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        f"id: {pack_name}\nname: {pack_name}\ndescription: per-culture-dir test pack\n",
        encoding="utf-8",
    )
    return pack_dir


def test_audit_walks_per_culture_directory_world(tmp_path: Path) -> None:
    """A world's ``cultures/<culture>.yaml`` directory model is discovered.

    The pack carries a genre-tier ``cultures.yaml`` (so the pack itself is
    detected pre-fix and this isolates the *walker* defect, not the
    pack-detection defect). Its single world uses the per-culture-dir
    model. Pre-fix: ``_audit_pack`` reads only ``worlds/<w>/cultures.yaml``
    (absent here) so the world culture's corpus never appears. Post-fix:
    the directory is walked and the corpus is surfaced under its culture.
    """
    pack_dir = _scaffold_pack(tmp_path, "synthdir")
    (pack_dir / "corpus" / "genre_corpus.txt").write_text(_corpus_words(1500), encoding="utf-8")
    (pack_dir / "corpus" / "dir_corpus.txt").write_text(_corpus_words(1500), encoding="utf-8")
    (pack_dir / "cultures.yaml").write_text(
        _genre_culture_list_yaml("Genre Culture", "genre_corpus.txt"), encoding="utf-8"
    )
    cdir = pack_dir / "worlds" / "testworld" / "cultures"
    cdir.mkdir(parents=True)
    (cdir / "dir_culture.yaml").write_text(
        _world_dir_culture_yaml("Dir Culture", "dir_corpus.txt"), encoding="utf-8"
    )

    result = _run_audit("--path", str(tmp_path / "genre_packs"))
    out = result.stdout

    # The directory-model culture's corpus must appear, attributed to its
    # consuming culture on the SAME row (co-location, not two loose
    # substrings) and tagged with the world tier.
    assert any(
        "Dir Culture" in line and "dir_corpus.txt" in line and "testworld" in line
        for line in out.splitlines()
    ), (
        "per-culture-dir world culture not discovered — the audit must walk "
        f"worlds/<w>/cultures/*.yaml like the loader does.\nstdout:\n{out}"
    )


def test_audit_detects_pack_with_only_per_culture_dir_world(tmp_path: Path) -> None:
    """A pack whose ONLY cultures are per-culture-dir worlds is not skipped.

    ``main()``'s pack-detection currently treats a directory as a pack only
    if it has a genre-tier ``cultures.yaml`` OR a world ``cultures.yaml``.
    A pack that authors cultures exclusively via ``worlds/<w>/cultures/``
    (no genre file, no world ``cultures.yaml``) is dropped wholesale
    pre-fix. Post-fix the per-culture-dir model also counts as "looks like
    a pack", so the world's corpus is audited.
    """
    pack_dir = _scaffold_pack(tmp_path, "synthdironly")
    (pack_dir / "corpus" / "only_corpus.txt").write_text(_corpus_words(1500), encoding="utf-8")
    cdir = pack_dir / "worlds" / "loneworld" / "cultures"
    cdir.mkdir(parents=True)
    (cdir / "only_culture.yaml").write_text(
        _world_dir_culture_yaml("Only Culture", "only_corpus.txt"), encoding="utf-8"
    )

    out = _run_audit("--path", str(tmp_path / "genre_packs")).stdout

    assert any(
        "Only Culture" in line and "only_corpus.txt" in line and "loneworld" in line
        for line in out.splitlines()
    ), (
        "a pack whose only cultures live in per-culture-dir worlds was skipped — "
        f"pack-detection must recognize worlds/<w>/cultures/ too.\nstdout:\n{out}"
    )


def test_audit_per_culture_dir_takes_precedence_over_cultures_yaml(tmp_path: Path) -> None:
    """When a world has BOTH a ``cultures/`` dir and ``cultures.yaml``, the dir wins.

    Mirrors the loader's ``if cultures_dir.is_dir(): ... else: <cultures.yaml>``
    branch (loader.py ~L895-914): the directory model REPLACES the single
    file, it does not merge. Pre-fix the audit reads only ``cultures.yaml``
    (so ``file_corpus.txt`` shows, ``dir_corpus.txt`` does not) — the exact
    inverse of correct. Post-fix: ``dir_corpus.txt`` shows and
    ``file_corpus.txt`` is ignored, matching the loader.
    """
    pack_dir = _scaffold_pack(tmp_path, "synthprec")
    (pack_dir / "corpus" / "dir_corpus.txt").write_text(_corpus_words(1500), encoding="utf-8")
    (pack_dir / "corpus" / "file_corpus.txt").write_text(_corpus_words(1500), encoding="utf-8")
    world_dir = pack_dir / "worlds" / "bothworld"
    (world_dir / "cultures").mkdir(parents=True)
    (world_dir / "cultures" / "dir_culture.yaml").write_text(
        _world_dir_culture_yaml("Dir Wins Culture", "dir_corpus.txt"), encoding="utf-8"
    )
    (world_dir / "cultures.yaml").write_text(
        _genre_culture_list_yaml("File Loses Culture", "file_corpus.txt"), encoding="utf-8"
    )

    out = _run_audit("--path", str(tmp_path / "genre_packs")).stdout

    assert "dir_corpus.txt" in out, (
        "the per-culture-dir model must win when both exist (loader parity); "
        f"dir_corpus.txt absent.\nstdout:\n{out}"
    )
    assert "file_corpus.txt" not in out, (
        "cultures.yaml must be IGNORED when a cultures/ directory is present "
        "(loader uses if/else, not merge); file_corpus.txt leaked into the "
        f"report.\nstdout:\n{out}"
    )


def test_audit_per_culture_dir_skips_gitkeep_and_nameless_overlays(tmp_path: Path) -> None:
    """The dir walk skips ``.gitkeep`` and no-``name`` art overlays, gracefully.

    The loader skips files whose parsed mapping lacks a ``name`` key —
    these are art-pipeline visual-token overlays that live in ``cultures/``
    for the daemon image pipeline, not name-generation cultures
    (loader.py L902-906). A naive ``Culture.model_validate`` on such a file
    would raise. The audit must skip them the way the loader does and still
    surface the real culture — not abort with a traceback (rc=2).
    """
    pack_dir = _scaffold_pack(tmp_path, "synthskip")
    (pack_dir / "corpus" / "real_corpus.txt").write_text(_corpus_words(1500), encoding="utf-8")
    cdir = pack_dir / "worlds" / "mixedworld" / "cultures"
    cdir.mkdir(parents=True)
    (cdir / "real_culture.yaml").write_text(
        _world_dir_culture_yaml("Real Culture", "real_corpus.txt"), encoding="utf-8"
    )
    (cdir / ".gitkeep").write_text("", encoding="utf-8")
    # Art-pipeline overlay: a mapping WITHOUT a "name" key. Must be skipped.
    (cdir / "_visual_overlay.yaml").write_text(
        "visual_tokens:\n  - chrome\n  - neon\n", encoding="utf-8"
    )

    result = _run_audit("--path", str(tmp_path / "genre_packs"))

    assert result.returncode in (0, 1), (
        f"audit aborted (rc={result.returncode}) on a cultures/ dir containing "
        f".gitkeep + a nameless overlay — it must skip them, not crash.\n"
        f"stderr:\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr, (
        f"audit raised on a nameless overlay instead of skipping it.\nstderr:\n{result.stderr}"
    )
    assert any(
        "Real Culture" in line and "real_corpus.txt" in line
        for line in result.stdout.splitlines()
    ), (
        "the real culture in a mixed cultures/ dir was not surfaced.\n"
        f"stdout:\n{result.stdout}"
    )


def test_audit_discovers_perseus_cloud_yulan_corpus() -> None:
    """Live tree: the perseus_cloud ``Yulan`` culture's corpora are audited.

    This is the named regression in the story title. ``perseus_cloud`` uses
    the per-culture-dir model, so pre-fix the audit never sees it and the
    yulan corpora (``yulan_given.txt`` / ``yulan_surname.txt``, which live
    in ``corpus/shared/``) are absent from the report. Post-fix both appear,
    attributed to the Yulan culture under the perseus_cloud world tier.
    """
    out = _run_audit().stdout
    assert out.strip(), "audit produced empty stdout — cannot assert on an absent report"

    for corpus_name in ("yulan_given.txt", "yulan_surname.txt"):
        assert any(
            "Yulan" in line and corpus_name in line and "perseus_cloud" in line
            for line in out.splitlines()
        ), (
            f"{corpus_name} not attributed to the Yulan culture under the "
            f"perseus_cloud world tier — the audit is blind to the "
            f"per-culture-dir world.\nstdout:\n{out}"
        )


def test_audit_discovers_aureate_span_world_cultures() -> None:
    """Live tree: aureate_span's per-culture-dir cultures are audited.

    ``aureate_span`` also uses the directory model. Asserts a concrete
    (culture, corpus) pair unique to the world tier — ``Span Aristocracy``
    consumes ``latin.txt`` — co-located with the aureate_span tier marker.
    Co-location with the world name distinguishes this from the genre-tier
    ``Hegemonic`` row that also consumes ``latin.txt``. Pre-fix: absent.
    """
    out = _run_audit().stdout
    assert out.strip(), "audit produced empty stdout — cannot assert on an absent report"

    assert any(
        "Span Aristocracy" in line and "latin.txt" in line and "aureate_span" in line
        for line in out.splitlines()
    ), (
        "aureate_span world cultures not discovered — the audit must walk "
        f"its cultures/ directory.\nstdout:\n{out}"
    )


def _loader_cultures_with_corpora(cultures: list) -> set[str]:
    """Names of loader-discovered cultures that reference at least one corpus.

    The audit only emits a row for a culture that consumes a corpus, so the
    parity comparison is restricted to that subset.
    """
    names: set[str] = set()
    for culture in cultures:
        for slot in culture.slots.values():
            if slot.corpora:
                names.add(culture.name)
                break
    return names


@pytest.mark.parametrize("world", ["perseus_cloud", "aureate_span"])
def test_audit_world_culture_discovery_matches_loader(world: str) -> None:
    """Wiring/parity: every corpus-bearing world culture the LOADER finds, the audit reports.

    This is the AC's "verified via wiring test" — a behavioral parity check,
    not a source-text grep (per CLAUDE.md "No Source-Text Wiring Tests").
    It loads ``space_opera`` through the real production loader
    (``load_genre_pack``), collects the cultures the loader discovers for
    ``world`` (which exercises the loader's per-culture-dir discovery), then
    runs the audit on the same live tree and asserts each such culture
    appears in the report under its world tier. If the audit's discovery
    diverges from the loader's, this fails — and it survives refactors of
    either implementation because it asserts on observed cultures, not code
    shape.

    Pre-fix: the audit reports none of these world cultures → fails.
    """
    from sidequest.genre.loader import load_genre_pack

    pack = load_genre_pack(SPACE_OPERA)
    assert world in pack.worlds, (
        f"loader did not surface world {world!r} (draft? renamed?); "
        "parity-test assumption broken."
    )
    expected = _loader_cultures_with_corpora(pack.worlds[world].cultures)
    assert expected, (
        f"loader found no corpus-referencing cultures in {world!r}; "
        "the parity test would be vacuous — fixture assumption broken."
    )

    out = _run_audit().stdout
    missing = [
        name
        for name in sorted(expected)
        if not any(name in line and world in line for line in out.splitlines())
    ]
    assert not missing, (
        f"audit discovery diverges from loader for world {world!r}: the loader "
        f"finds these corpus-bearing cultures but the audit does not report them "
        f"under the world tier: {missing}.\nstdout:\n{out}"
    )


def test_audit_live_tree_stays_zero_with_per_culture_dir_worlds() -> None:
    """Live tree: rc=0 holds AFTER per-culture-dir worlds are folded in (AC: no regression).

    Discovering aureate_span + perseus_cloud must not flip the exit code:
    every corpus they reference resolves (pack ``corpus/`` or the
    ``corpus/shared/`` fallback) and clears the FAIL floor — yulan_given /
    yulan_surname are THIN, which is a warning, not a CI gate. This pins
    that the discovery fix surfaces the worlds WITHOUT introducing a
    spurious FAIL/MISSING. The co-discovery assertion guards against a
    vacuous pass where rc=0 simply because the worlds are still invisible.
    """
    result = _run_audit()
    out = result.stdout
    assert "perseus_cloud" in out and "aureate_span" in out, (
        "per-culture-dir worlds still absent from the report — rc=0 here would "
        f"be the old blindness, not a clean post-fix tree.\nstdout:\n{out}"
    )
    assert result.returncode == 0, (
        f"folding in per-culture-dir worlds must keep the live tree at rc=0 "
        f"(their corpora all resolve; yulan is THIN, not FAIL). got rc="
        f"{result.returncode}.\nstdout:\n{out}\nstderr:\n{result.stderr}"
    )
