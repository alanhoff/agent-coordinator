"""Validated, deterministic role/model/effort selection."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from coordinator.state.store import EFFORTS, MODELS, ROLES

STAGES = {
    "architecture": "architect",
    "design": "designer",
    "documentation": "documenter",
    "fix": "fixer",
    "implementation": "implementer",
    "integration": "implementer",
    "research": "researcher",
    "review": "reviewer",
    "validation": "validator",
}
MODEL_CAPACITY = {"gpt-5.6-luna": 1.8, "gpt-5.6-terra": 3.5, "gpt-5.6-sol": 5.0}
MODEL_COST = {"gpt-5.6-luna": 1.0, "gpt-5.6-terra": 4.0, "gpt-5.6-sol": 10.0}
EFFORT_CAPACITY = {"none": -0.8, "low": -0.4, "medium": 0.0, "high": 0.4, "xhigh": 0.7, "max": 1.0}
EFFORT_COST = {"none": 0.5, "low": 0.7, "medium": 1.0, "high": 1.4, "xhigh": 1.9, "max": 2.6}


class RoutingError(ValueError):
    pass


def _score(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 1 <= value <= 5:
        raise RoutingError(f"{field} must be a number from 1 through 5")
    return float(value)


def validate_task(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutingError("task file must contain one JSON object")
    expected = {
        "summary", "stage", "complexity", "ambiguity", "criticality", "coupling", "novelty",
        "determinism", "input_tokens", "expected_output_tokens",
    }
    unknown = set(value) - expected
    required = expected - {"input_tokens", "expected_output_tokens"}
    missing = required - set(value)
    if unknown or missing:
        raise RoutingError(
            "task fields are invalid"
            + ("; missing " + ", ".join(sorted(missing)) if missing else "")
            + ("; unknown " + ", ".join(sorted(unknown)) if unknown else "")
        )
    if (
        not isinstance(value["summary"], str)
        or not value["summary"].strip()
        or len(value["summary"]) > 32_768
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value["summary"])
    ):
        raise RoutingError("summary must be a non-blank bounded string")
    if not isinstance(value["stage"], str) or value["stage"] not in STAGES:
        raise RoutingError("stage is not supported")
    result = {"summary": value["summary"], "stage": value["stage"]}
    for field in ("complexity", "ambiguity", "criticality", "coupling", "novelty", "determinism"):
        result[field] = _score(value[field], field)
    for field, default in (("input_tokens", 25_000), ("expected_output_tokens", 2_000)):
        current = value.get(field, default)
        if not isinstance(current, int) or isinstance(current, bool) or current < 0 or current > 2_000_000:
            raise RoutingError(f"{field} must be a bounded non-negative integer")
        result[field] = current
    return result


def validate_profile(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "allowed_models": list(MODELS),
            "allowed_efforts": list(EFFORTS),
            "budget": "balanced",
        }
    if not isinstance(value, dict):
        raise RoutingError("profile file must contain one JSON object")
    expected = {"allowed_models", "allowed_efforts", "budget"}
    if set(value) - expected:
        raise RoutingError("profile contains unknown fields: " + ", ".join(sorted(set(value) - expected)))
    models = value.get("allowed_models", list(MODELS))
    efforts = value.get("allowed_efforts", list(EFFORTS))
    budget = value.get("budget", "balanced")
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(item, str) for item in models)
        or len(set(models)) != len(models)
        or any(item not in MODELS for item in models)
    ):
        raise RoutingError("allowed_models must be a unique non-empty list of supported models")
    if (
        not isinstance(efforts, list)
        or not efforts
        or any(not isinstance(item, str) for item in efforts)
        or len(set(efforts)) != len(efforts)
        or any(item not in EFFORTS for item in efforts)
    ):
        raise RoutingError("allowed_efforts must be a unique non-empty list of supported efforts")
    if budget not in ("value", "balanced", "quality"):
        raise RoutingError("budget must be value, balanced, or quality")
    return {"allowed_models": models, "allowed_efforts": efforts, "budget": budget}


def choose(task_value: Any, profile_value: Any = None) -> dict[str, Any]:
    task = validate_task(task_value)
    profile = validate_profile(profile_value)
    required = (
        task["complexity"] * 0.23
        + task["ambiguity"] * 0.20
        + task["criticality"] * 0.24
        + task["coupling"] * 0.14
        + task["novelty"] * 0.14
        + (6 - task["determinism"]) * 0.05
    )
    if task["stage"] in ("architecture", "review"):
        required += 0.3
    if task["stage"] in ("documentation", "validation"):
        required -= 0.2
    cost_weight = {"value": 0.35, "balanced": 0.14, "quality": 0.04}[profile["budget"]]
    alternatives = []
    for model in profile["allowed_models"]:
        for effort in profile["allowed_efforts"]:
            capacity = MODEL_CAPACITY[model] + EFFORT_CAPACITY[effort]
            margin = capacity - required
            relative_cost = MODEL_COST[model] * EFFORT_COST[effort]
            score = margin - cost_weight * relative_cost - (abs(margin) * 0.08 if margin >= 0 else abs(margin) * 3.0)
            alternatives.append(
                {
                    "model": model,
                    "effort": effort,
                    "capacity": round(capacity, 3),
                    "required_capacity": round(required, 3),
                    "margin": round(margin, 3),
                    "relative_cost": round(relative_cost, 3),
                    "score": round(score, 3),
                    "viable": margin >= 0,
                }
            )
    alternatives.sort(key=lambda item: (item["viable"], item["score"]), reverse=True)
    selected = next((item for item in alternatives if item["viable"]), alternatives[0])
    selection = (
        "highest ranked allowed viable route"
        if selected["viable"]
        else "highest ranked allowed fallback because no allowed route met required capacity"
    )
    canonical = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    role = STAGES[task["stage"]]
    if role not in ROLES:
        raise RoutingError("selected role is not installed")
    return {
        "task_digest": hashlib.sha256(canonical).hexdigest(),
        "route": {"role": role, "model": selected["model"], "effort": selected["effort"]},
        "rationale": (
            f"{task['stage']} requires capacity {required:.2f}; selected the {selection} "
            f"under the {profile['budget']} profile"
        ),
        "inputs": task,
        "profile": profile,
        "alternatives": alternatives[:5],
        "caveat": "Capacity and relative cost are deterministic planning heuristics, not price or quality guarantees.",
    }
