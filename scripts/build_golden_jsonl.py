"""Build ``eval/golden.jsonl`` from ``data/triage_notes.csv``.

This script is the Phase 8 A1 deliverable. It is a pure transformation:
``triage_notes.csv`` carries the human labels (hand-edited) and is the
single source of truth; ``golden.jsonl`` is a reproducible build
artifact validated against ``backend.app.schemas.GoldenCase``.

See ``docs/testing.md`` § Golden Set for the build rules and the
``Fact`` shape table.

Usage::

    python scripts/build_golden_jsonl.py
    python scripts/build_golden_jsonl.py --check

``--check`` exits non-zero (with a clear stderr message) when the CSV
is newer than the JSONL, or when the regenerated JSONL would differ
from the on-disk copy. It does **not** rewrite anything; it is the CI
soft gate that catches "you edited triage_notes.csv but forgot to
rebuild golden.jsonl".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

# Add the repo root to sys.path so ``from backend.app.schemas import ...``
# works whether the script is launched from the repo root, a subdirectory,
# or via ``python -m scripts.build_golden_jsonl``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.schemas import (  # noqa: E402  (path setup above)
    GoldenCase,
    V1_FAILURE_VOCABULARY,
)

CSV_PATH = _REPO_ROOT / "data" / "triage_notes.csv"
JSONL_PATH = _REPO_ROOT / "eval" / "golden.jsonl"

# Steps within +/- this many of the labelled failure step are accepted by
# clause 3 of the judge rubric. Mirrors the +/-2 tolerance used by
# backend.app.agent_eval.failure_step_within_2.
FAILURE_STEP_TOLERANCE = 2


def _parse_failure_types(raw: str) -> list[str]:
    """Split a multi-label ``failure_mode`` CSV cell on ``;``.

    Empty cells return ``[]``; whitespace around each token is stripped
    defensively (the CSV occasionally has stray spaces after a ``;``).
    Unknown failure types raise — the build refuses to encode a label
    the v1 vocabulary cannot represent. That keeps the schema honest.
    """
    if not raw or not raw.strip():
        return []
    tokens = [t.strip() for t in raw.split(";") if t.strip()]
    unknown = [t for t in tokens if t not in V1_FAILURE_VOCABULARY]
    if unknown:
        raise ValueError(
            f"failure_mode cell contains unknown failure types: {unknown!r}; "
            f"allowed: {V1_FAILURE_VOCABULARY!r}"
        )
    return tokens


def _parse_failure_step(raw: str) -> int | None:
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"failure_step must be an integer or empty; got {raw!r}") from exc


def build_row(row: dict[str, str]) -> dict[str, Any]:
    """Translate one CSV row into a ``GoldenCase``-shaped dict.

    The returned dict is validated against ``GoldenCase`` before being
    written, so any rule violation raises here rather than producing a
    silently-malformed JSONL file.
    """
    run_id = (row.get("sample_id") or "").strip()
    category = (row.get("category") or "").strip().lower()
    outcome = (row.get("outcome") or "").strip().lower()
    failure_types = _parse_failure_types(row.get("failure_mode") or "")
    failure_step = _parse_failure_step(row.get("failure_step") or "")

    if not run_id:
        raise ValueError(f"row missing sample_id: {row!r}")
    if not category:
        raise ValueError(f"row missing category: {row!r}")
    if outcome not in {"success", "failed"}:
        raise ValueError(f"row outcome must be 'success' or 'failed'; got {outcome!r}")

    if outcome == "success":
        # Success shape: only outcome facts; no failure_type / failure_step.
        expected_facts: list[dict[str, Any]] = [
            {"field": "outcome", "op": "eq", "value": "success"},
        ]
        forbidden_facts: list[dict[str, Any]] = [
            {"field": "outcome", "op": "eq", "value": "failed"},
        ]
        tags = [category]
    else:
        if not failure_types:
            raise ValueError(
                f"failed-outcome row must carry at least one failure_mode "
                f"label; got {row!r}"
            )
        expected_facts = [
            {"field": "outcome", "op": "eq", "value": "failed"},
            {"field": "failure_type", "op": "in", "value": failure_types},
        ]
        if failure_step is not None:
            lo = max(0, failure_step - FAILURE_STEP_TOLERANCE)
            hi = failure_step + FAILURE_STEP_TOLERANCE
            expected_facts.append(
                {"field": "failure_step", "op": "in_range", "value": [lo, hi]}
            )
        # forbidden_facts: everything failed cannot be: the success outcome,
        # and any failure type not in the labelled multi-label set.
        forbidden_failure_types = [
            t for t in V1_FAILURE_VOCABULARY if t not in set(failure_types)
        ]
        forbidden_facts = [
            {"field": "outcome", "op": "eq", "value": "success"},
        ]
        if forbidden_failure_types:
            forbidden_facts.append(
                {
                    "field": "failure_type",
                    "op": "in",
                    "value": forbidden_failure_types,
                }
            )
        tags = [category, *failure_types]

    payload = {
        "input": {"run_id": run_id, "intent": "analyze_run"},
        "expected_facts": expected_facts,
        "forbidden_facts": forbidden_facts,
        "tags": tags,
    }
    # Validate. Raises if any rule (e.g. expected/forbidden overlap) was violated.
    GoldenCase.model_validate(payload)
    return payload


def build_jsonl(csv_path: Path = CSV_PATH) -> list[dict[str, Any]]:
    """Read the CSV and return the in-order list of GoldenCase dicts."""
    if not csv_path.exists():
        raise FileNotFoundError(f"triage CSV not found: {csv_path}")
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return [build_row(row) for row in rows]


def serialise(cases: list[dict[str, Any]]) -> str:
    """JSONL serialisation. One row per line, deterministic key order via
    ``sort_keys`` so re-running the build produces byte-identical output
    (the ``--check`` guard depends on this)."""
    return "".join(
        json.dumps(case, sort_keys=True, ensure_ascii=False) + "\n"
        for case in cases
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build eval/golden.jsonl from data/triage_notes.csv."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the on-disk JSONL is stale or missing; "
        "do not modify anything. Intended for CI.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=CSV_PATH,
        help=f"Path to the triage CSV. Default: {CSV_PATH.relative_to(_REPO_ROOT)}",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=JSONL_PATH,
        help=f"Path to the JSONL output. Default: {JSONL_PATH.relative_to(_REPO_ROOT)}",
    )
    args = parser.parse_args(argv)

    cases = build_jsonl(args.csv)
    rendered = serialise(cases)

    if args.check:
        if not args.out.exists():
            print(
                f"check FAILED: {args.out.relative_to(_REPO_ROOT)} does not exist; "
                f"run `python scripts/build_golden_jsonl.py` to create it.",
                file=sys.stderr,
            )
            return 1
        on_disk = args.out.read_text(encoding="utf-8")
        if on_disk != rendered:
            print(
                f"check FAILED: {args.out.relative_to(_REPO_ROOT)} is stale; "
                f"run `python scripts/build_golden_jsonl.py` to regenerate it.",
                file=sys.stderr,
            )
            return 1
        print(
            f"check OK: {args.out.relative_to(_REPO_ROOT)} matches "
            f"{args.csv.relative_to(_REPO_ROOT)} ({len(cases)} rows)."
        )
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {args.out.relative_to(_REPO_ROOT)}: {len(cases)} rows."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
