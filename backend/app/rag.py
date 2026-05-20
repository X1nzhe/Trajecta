from __future__ import annotations

import re

from .schemas import FailureMemoryCase


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def score_case_similarity(query: str, case: FailureMemoryCase) -> float:
    q = _tokenize(query)
    c = _tokenize(" ".join([case.failure_label, case.summary, *case.tags]))
    if not q or not c:
        return 0.0
    return len(q & c) / len(q | c)


def retrieve_similar_cases(
    query: str, cases: list[FailureMemoryCase], top_k: int = 3
) -> list[FailureMemoryCase]:
    ranked = sorted(cases, key=lambda case: score_case_similarity(query, case), reverse=True)
    return [case for case in ranked[:top_k] if score_case_similarity(query, case) > 0]
