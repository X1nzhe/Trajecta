"""Phase 8 A1 — tests for the golden set build path.

Covers the five assertions docs/testing.md § "tests/test_golden_set.py"
spells out, plus a couple of Pydantic-layer sanity tests on the
``GoldenCase`` discriminated union so a future refactor of the build
script cannot silently relax the schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.schemas import (
    FailureStepFact,
    FailureTypeFact,
    GoldenCase,
    OutcomeFact,
    V1_FAILURE_VOCABULARY,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_golden_jsonl.py"
GOLDEN_PATH = REPO_ROOT / "eval" / "golden.jsonl"
CSV_PATH = REPO_ROOT / "data" / "triage_notes.csv"

EXPECTED_CATEGORIES = {
    "allrecipes",
    "amazon",
    "apple",
    "arxiv",
    "booking",
    "github",
    "google_flight",
    "huggingface",
}


def _load_rows() -> list[dict]:
    """Load eval/golden.jsonl as a list of dicts.

    Skipped (not failed) when the artefact is missing so a fresh clone
    that hasn't run the build script yet does not fail the suite — the
    artefact is gitignored-then-regenerated path is supported.
    """
    if not GOLDEN_PATH.exists():
        pytest.skip(
            f"{GOLDEN_PATH.relative_to(REPO_ROOT)} is missing; "
            f"run `python scripts/build_golden_jsonl.py` first."
        )
    with GOLDEN_PATH.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Build-script smoke tests


def test_build_script_produces_35_rows() -> None:
    """docs/testing.md § Golden Set acceptance: 35 rows present."""
    rows = _load_rows()
    assert len(rows) == 35, f"expected 35 rows, got {len(rows)}"


def test_every_row_validates_as_GoldenCase() -> None:
    """docs/testing.md tests/test_golden_set.py:
    'every row validates against the GoldenCase Pydantic model'."""
    rows = _load_rows()
    for i, row in enumerate(rows):
        # Raises on validation failure with a precise field path; pytest
        # surfaces it directly so failures are debuggable.
        GoldenCase.model_validate(row), f"row {i} failed validation"


def test_expected_and_forbidden_facts_are_disjoint() -> None:
    """docs/testing.md tests/test_golden_set.py:
    'expected_facts and forbidden_facts are disjoint for every row'.

    Disjointness is also enforced by ``GoldenCase._validate_shape``, so
    a violation here implies the Pydantic guard has been bypassed.
    """
    rows = _load_rows()
    for i, row in enumerate(rows):
        case = GoldenCase.model_validate(row)
        expected_keys = {case._fact_key(f) for f in case.expected_facts}
        forbidden_keys = {case._fact_key(f) for f in case.forbidden_facts}
        overlap = expected_keys & forbidden_keys
        assert not overlap, f"row {i} has overlapping facts: {sorted(overlap)}"


def test_all_eight_categories_present_in_tags() -> None:
    """docs/testing.md tests/test_golden_set.py:
    'all 8 categories appear in the tag column'."""
    rows = _load_rows()
    seen_tags: set[str] = set()
    for row in rows:
        seen_tags.update(row["tags"])
    missing = EXPECTED_CATEGORIES - seen_tags
    assert not missing, f"missing category tags: {sorted(missing)}"


def test_check_mode_reports_clean_state() -> None:
    """`--check` exits 0 when CSV and JSONL agree.

    docs/testing.md tests/test_golden_set.py:
    'check exits non-zero when triage_notes.csv is newer than
    golden.jsonl'. The reverse — clean state — is what most CI runs
    encounter, so we cover it explicitly here. The failure case is
    covered in test_check_mode_detects_drift below.
    """
    if not GOLDEN_PATH.exists():
        pytest.skip("golden.jsonl missing; run build script first")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"--check should exit 0 on a clean build; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_check_mode_detects_drift(tmp_path: Path) -> None:
    """`--check` exits non-zero when the on-disk JSONL diverges from
    what the CSV would produce.

    We don't mutate the repo's triage_notes.csv — instead we run
    --check against a tampered JSONL at a tmp path with the real CSV.
    """
    rows = _load_rows()
    # Drop the last row to simulate drift between CSV and JSONL.
    tampered = tmp_path / "golden.jsonl"
    with tampered.open("w", encoding="utf-8") as f:
        for row in rows[:-1]:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--check",
            "--csv",
            str(CSV_PATH),
            "--out",
            str(tampered),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "--check should exit non-zero when the JSONL is stale"
    )
    assert "stale" in result.stderr.lower() or "stale" in result.stdout.lower()


# ---------------------------------------------------------------------------
# GoldenCase / Fact unit tests (independent of the on-disk artefact)


def test_failure_type_fact_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown failure types"):
        FailureTypeFact(field="failure_type", op="in", value=["bogus_type"])


def test_failure_step_fact_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="min <= max"):
        FailureStepFact(field="failure_step", op="in_range", value=(10, 5))


def test_failure_step_fact_rejects_negative_bounds() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        FailureStepFact(field="failure_step", op="in_range", value=(-1, 3))


def test_success_shape_rejects_failure_facts() -> None:
    """A success row must not carry FailureTypeFact / FailureStepFact in
    expected_facts (otherwise the success/failure shape boundary blurs
    and the judge has nothing meaningful to check on clause 2)."""
    with pytest.raises(ValueError, match="success-shape"):
        GoldenCase.model_validate(
            {
                "input": {"run_id": "abc"},
                "expected_facts": [
                    {"field": "outcome", "op": "eq", "value": "success"},
                    {
                        "field": "failure_type",
                        "op": "in",
                        "value": ["missed_constraint"],
                    },
                ],
                "forbidden_facts": [
                    {"field": "outcome", "op": "eq", "value": "failed"}
                ],
                "tags": ["x"],
            }
        )


def test_failed_shape_requires_failure_type_fact() -> None:
    """A failed row must include a FailureTypeFact in expected_facts.
    Without it, judge clause 2 has nothing to match against."""
    with pytest.raises(ValueError, match="failed-shape"):
        GoldenCase.model_validate(
            {
                "input": {"run_id": "abc"},
                "expected_facts": [
                    {"field": "outcome", "op": "eq", "value": "failed"}
                ],
                "forbidden_facts": [
                    {"field": "outcome", "op": "eq", "value": "success"}
                ],
                "tags": ["x"],
            }
        )


def test_outcome_fact_round_trips_through_jsonl() -> None:
    """Serialise + deserialise preserves the discriminated union."""
    case = GoldenCase.model_validate(
        {
            "input": {"run_id": "abc"},
            "expected_facts": [
                {"field": "outcome", "op": "eq", "value": "success"}
            ],
            "forbidden_facts": [
                {"field": "outcome", "op": "eq", "value": "failed"}
            ],
            "tags": ["x"],
        }
    )
    payload = case.model_dump(mode="json")
    rehydrated = GoldenCase.model_validate(payload)
    assert isinstance(rehydrated.expected_facts[0], OutcomeFact)
    assert rehydrated.expected_facts[0].value == "success"


def test_v1_failure_vocabulary_size_is_five() -> None:
    """Anchor test: if the vocabulary grows past 5, downstream judge code
    (clause 2 multi-label OR, ``failed-shape`` validators) needs review.
    Catching it here makes the dependency explicit."""
    assert len(V1_FAILURE_VOCABULARY) == 5
