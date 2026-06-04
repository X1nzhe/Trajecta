"""Gate committed eval metrics against regression floors (CI threshold gate).

This is the S18 §2.2 optional-bonus "wire a metric into CI as a threshold
gate". It is a *regression gate over committed artifacts*, not a live
recompute: it reads report JSON that is already tracked in the repo and
exits non-zero when a headline metric has dropped below its floor. That
keeps the gate deterministic, free, and key-free — the semantic metrics
(RAGAS faithfulness, dual-judge kappa) are billed/rate-limited/non-
deterministic to recompute, so they are recomputed deliberately offline
(``ragas_eval`` / ``agent_eval --judge``) and *gated* here.

Sources (all git-tracked):
  - RAGAS faithfulness      : eval/ragas_report.json (metric_means.faithfulness;
                              also requires ragas_mode == "real")
  - dual-judge kappa        : eval/runs/<FEATURED_RUN>/judge/judge_agreement_report.json
                              (kappa_llm_llm; the file embeds its own kappa_threshold)
  - agent binary accuracy   : eval/runs/<FEATURED_RUN>/agent_report.json
                              (metrics.binary_verdict_accuracy)

Floors sit a margin below the current recorded values so the gate catches
real regressions, not run-to-run noise. The per-run ``agent_report.json``
is used (stable, whitelisted) rather than the rolling root copy.

Usage::

    python scripts/check_eval_thresholds.py
    python scripts/check_eval_thresholds.py --faithfulness-min 0.999   # demo a failure

Exits 0 when every metric clears its floor, 1 otherwise (with a clear
``actual vs floor`` message on stderr per failing check). Missing files or
missing keys also fail loud.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

# README "featured" v6-mini run that carries the judged dual-judge artifacts.
# Bump this (and re-confirm floors) when a newer blessed run replaces it.
FEATURED_RUN = "2026-06-03T05-45-39Z"

RAGAS_REPORT = _REPO_ROOT / "eval" / "ragas_report.json"
JUDGE_REPORT = (
    _REPO_ROOT / "eval" / "runs" / FEATURED_RUN / "judge" / "judge_agreement_report.json"
)
AGENT_REPORT = _REPO_ROOT / "eval" / "runs" / FEATURED_RUN / "agent_report.json"

# Regression floors. Current recorded values are noted alongside.
FAITHFULNESS_MIN = 0.85  # current 0.957 (eval/ragas_report.json, n=58, real)
KAPPA_MIN = 0.60  # S18 target; current 1.0 on the featured run
BINARY_ACC_MIN = 0.75  # current 0.806 (featured run)


def _rel(path: Path) -> str:
    """Repo-relative display path, falling back to the raw path when the
    target lives outside the repo (e.g. an explicit ``--report`` override)."""
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"artifact not found: {_rel(path)}")
    return json.loads(path.read_text(encoding="utf-8"))


def _dig(obj: dict[str, Any], *keys: str, source: Path) -> Any:
    """Fetch a nested key, raising a clear error naming the source file."""
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(
                f"missing key {'.'.join(keys)!r} in {_rel(source)}"
            )
        cur = cur[key]
    return cur


def check_faithfulness(report: Path, floor: float) -> tuple[bool, str]:
    data = _load(report)
    mode = _dig(data, "ragas_mode", source=report)
    if mode != "real":
        return (
            False,
            f"faithfulness: ragas_mode={mode!r}, expected 'real' "
            f"(a mock/fallback RAGAS run cannot satisfy the gate)",
        )
    value = float(_dig(data, "metric_means", "faithfulness", source=report))
    ok = value >= floor
    return ok, f"faithfulness: {value:.3f} {'>=' if ok else '<'} {floor:.3f} (floor)"


def check_kappa(report: Path, floor: float) -> tuple[bool, str]:
    data = _load(report)
    value = float(_dig(data, "kappa_llm_llm", source=report))
    # The judge report embeds its own threshold; honour the stricter of the two.
    embedded = data.get("kappa_threshold")
    effective = max(floor, float(embedded)) if embedded is not None else floor
    ok = value >= effective
    return ok, f"kappa_llm_llm: {value:.3f} {'>=' if ok else '<'} {effective:.3f} (floor)"


def check_binary_accuracy(report: Path, floor: float) -> tuple[bool, str]:
    data = _load(report)
    value = float(_dig(data, "metrics", "binary_verdict_accuracy", source=report))
    ok = value >= floor
    return ok, f"binary_verdict_accuracy: {value:.3f} {'>=' if ok else '<'} {floor:.3f} (floor)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate committed eval metrics against regression floors."
    )
    parser.add_argument("--ragas", type=Path, default=RAGAS_REPORT)
    parser.add_argument("--judge", type=Path, default=JUDGE_REPORT)
    parser.add_argument("--report", type=Path, default=AGENT_REPORT)
    parser.add_argument("--faithfulness-min", type=float, default=FAITHFULNESS_MIN)
    parser.add_argument("--kappa-min", type=float, default=KAPPA_MIN)
    parser.add_argument("--binary-acc-min", type=float, default=BINARY_ACC_MIN)
    args = parser.parse_args(argv)

    checks = (
        ("RAGAS faithfulness", check_faithfulness, args.ragas, args.faithfulness_min),
        ("dual-judge kappa", check_kappa, args.judge, args.kappa_min),
        ("agent binary accuracy", check_binary_accuracy, args.report, args.binary_acc_min),
    )

    results: list[tuple[str, bool, str]] = []
    failed = False
    for label, fn, path, floor in checks:
        try:
            ok, detail = fn(path, floor)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            ok, detail = False, str(exc)
        results.append((label, ok, detail))
        failed = failed or not ok

    width = max(len(label) for label, _, _ in results)
    for label, ok, detail in results:
        marker = "PASS" if ok else "FAIL"
        line = f"[{marker}] {label.ljust(width)}  {detail}"
        print(line, file=sys.stderr if not ok else sys.stdout)

    if failed:
        print(
            "\neval threshold gate FAILED: a committed metric is below its floor "
            "(or its artifact is missing/invalid).",
            file=sys.stderr,
        )
        return 1
    print("\neval threshold gate OK: all committed metrics clear their floors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
