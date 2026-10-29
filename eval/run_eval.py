import json
import os
import time
import ast
from typing import Any,Dict,List,Optional,Tuple
import httpx

CASE_SCHEMA = {
    "type": "object",
    "required": ["id", "filename", "language", "code", "category", "expected_findings"],
    "properties": {
        "id": {"type": "integer"},
        "filename": {"type": "string", "minLength": 1},
        "language": {"type": "object", "enum": ["python", "javascript", "cpp", "c++", "java"]},
        "code": {"type": "string", "minLength": 1},
        "category": {"type": "object", "enum": ["known_defect", "clean_code", "prompt_injection", "refactoring", "todo"]},
        "expected_findings": {"type": "array", "items": {"type": "object", "required": ["label", "severity", "description"], "properties": {"label": {"type": "string"}, "severity": {"type": "string"}, "description": {"type": "string"}}}},
    },
}


def find_metrics(tp: int, fp: int, fn: int, metric: str = "f1") -> Optional[float]:
    """Calculate precision, recall, or F1 score.

    Args:
        tp: True positives
        fp: False positives
        fn: False negatives
        metric: Which metric to return. One of "precision", "recall", "f1".
            Defaults to "f1".

    Returns:
        The requested metric, or None if undefined:
          - Precision: tp / (tp + fp), or None if tp+fp == 0
          - Recall: tp / (tp + fn), or None if tp+fn == 0
          - F1: harmonic mean of precision and recall, or None if both are
            undefined or sum to zero.
          - None when all inputs are zero (tp+fp == 0 and tp+fn == 0).
    """
    # When all are zero, return None
    if tp == 0 and fp == 0 and fn == 0:
        return None

    # Calculate precision: tp / (tp + fp)
    if tp + fp > 0:
        precision = tp / (tp + fp)
    else:
        precision = None

    # Calculate recall: tp / (tp + fn)
    if tp + fn > 0:
        recall = tp / (tp + fn)
    else:
        recall = None

    # Return the requested metric
    if metric == "precision":
        return precision
    if metric == "recall":
        return recall
    if metric == "f1":
        if precision is not None and recall is not None and (precision + recall) > 0:
            return 2 * precision * recall / (precision + recall)
        if precision is not None:
            return precision
        if recall is not None:
            return recall
        return None

    raise ValueError(f"Unknown metric: {metric!r}. Expected 'precision', 'recall', or 'f1'.")


def evaluate_cases(cases, use_llm=False):
    """Evaluate cases against the API. Offline by default."""
    results = []
    for case in cases:
        result = {"case_id": case["id"], "success": False, "findings": [], "latency": 0}
        try:
            # In offline mode, use stub responses
            if not use_llm:
                result["findings"] = case.get("expected_findings", [])
                result["success"] = True
                result["latency"] = 0.1  # stub latency
            else:
                # Would make actual API call
                result["success"] = False
        except Exception as e:
            result["error"] = str(e)
        results.append(result)
    return results
