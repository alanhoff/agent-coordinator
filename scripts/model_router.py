#!/usr/bin/env python3
"""Transparent ROI helper for selecting Codex child model and reasoning effort.

The prices are official standard API token rates captured on 2026-08-05.
OpenAI does not publish a fixed billing multiplier for reasoning effort. The
EFFORTS output factors below are planning heuristics, not price-sheet rates.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MODEL_ORDER: Tuple[str, ...] = (
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
)

MODELS: Mapping[str, Mapping[str, float]] = {
    "gpt-5.6-luna": {
        "input": 0.20,
        "cached_input": 0.02,
        "cache_write": 0.25,
        "output": 1.20,
        "long_input": 0.40,
        "long_cached_input": 0.04,
        "long_cache_write": 0.50,
        "long_output": 1.80,
        "capacity": 1.90,
        "latency": 0.70,
    },
    "gpt-5.6-terra": {
        "input": 2.00,
        "cached_input": 0.20,
        "cache_write": 2.50,
        "output": 12.00,
        "long_input": 4.00,
        "long_cached_input": 0.40,
        "long_cache_write": 5.00,
        "long_output": 18.00,
        "capacity": 3.65,
        "latency": 1.35,
    },
    "gpt-5.6-sol": {
        "input": 5.00,
        "cached_input": 0.50,
        "cache_write": 6.25,
        "output": 30.00,
        "long_input": 10.00,
        "long_cached_input": 1.00,
        "long_cache_write": 12.50,
        "long_output": 45.00,
        "capacity": 5.00,
        "latency": 2.20,
    },
}

EFFORT_ORDER: Tuple[str, ...] = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

EFFORTS: Mapping[str, Mapping[str, float]] = {
    # quality_uplift, expected output/reasoning-token factor, relative latency
    "none": {"quality_uplift": -0.75, "output_factor": 0.55, "latency": 0.45},
    "low": {"quality_uplift": -0.35, "output_factor": 0.75, "latency": 0.70},
    "medium": {"quality_uplift": 0.00, "output_factor": 1.00, "latency": 1.00},
    "high": {"quality_uplift": 0.35, "output_factor": 1.40, "latency": 1.35},
    "xhigh": {"quality_uplift": 0.65, "output_factor": 1.90, "latency": 1.80},
    "max": {"quality_uplift": 0.90, "output_factor": 2.60, "latency": 2.35},
}

STAGE_ADJUSTMENT: Mapping[str, float] = {
    "research": -0.10,
    "design": 0.10,
    "architecture": 0.35,
    "documentation": -0.35,
    "implementation": 0.15,
    "review": 0.35,
    "fix": 0.00,
    "validation": -0.25,
    "integration": 0.20,
}

STAGE_AFFINITY: Mapping[str, Mapping[str, float]] = {
    "gpt-5.6-luna": {
        "research": 0.05,
        "documentation": 0.20,
        "validation": 0.20,
        "fix": 0.05,
    },
    "gpt-5.6-terra": {
        "research": 0.15,
        "design": 0.15,
        "architecture": 0.10,
        "documentation": 0.15,
        "implementation": 0.15,
        "review": 0.10,
        "fix": 0.15,
        "validation": 0.10,
        "integration": 0.10,
    },
    "gpt-5.6-sol": {
        "design": 0.10,
        "architecture": 0.25,
        "implementation": 0.15,
        "review": 0.25,
        "integration": 0.20,
    },
}

BUDGET_WEIGHTS: Mapping[str, Mapping[str, float]] = {
    "value": {"cost": 1.45, "latency": 0.40, "margin": 0.55},
    "balanced": {"cost": 0.80, "latency": 0.25, "margin": 0.90},
    "quality": {"cost": 0.30, "latency": 0.10, "margin": 1.35},
}

LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class Candidate:
    model: str
    reasoning_effort: str
    estimated_cost_usd: float
    quality_proxy: float
    required_quality: float
    quality_margin: float
    relative_latency: float
    score: float
    viable: bool
    long_context_rates: bool


def _bounded(name: str, value: float, low: float = 1.0, high: float = 5.0) -> float:
    if value < low or value > high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}; got {value:g}")
    return value


def _rates(
    model: str, input_tokens: int, force_long: str
) -> Tuple[float, float, float, float, bool]:
    data = MODELS[model]
    if force_long == "yes":
        long_context = True
    elif force_long == "no":
        long_context = False
    else:
        long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    if long_context:
        return (
            data["long_input"],
            data["long_cached_input"],
            data["long_cache_write"],
            data["long_output"],
            True,
        )
    return (
        data["input"],
        data["cached_input"],
        data["cache_write"],
        data["output"],
        False,
    )


def estimate_cost(
    model: str,
    effort: str,
    input_tokens: int,
    base_output_tokens: int,
    cached_fraction: float = 0.0,
    force_long: str = "auto",
    cache_write_fraction: float = 0.0,
) -> Tuple[float, int, bool]:
    if model not in MODELS:
        raise ValueError(f"unknown model: {model}")
    if effort not in EFFORTS:
        raise ValueError(f"unknown reasoning effort: {effort}")
    if input_tokens < 0 or base_output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if cached_fraction < 0.0 or cached_fraction > 1.0:
        raise ValueError("cached_fraction must be between 0 and 1")
    if cache_write_fraction < 0.0 or cache_write_fraction > 1.0:
        raise ValueError("cache_write_fraction must be between 0 and 1")
    if cached_fraction + cache_write_fraction > 1.0:
        raise ValueError("cached_fraction + cache_write_fraction must not exceed 1")

    input_rate, cached_rate, cache_write_rate, output_rate, is_long = _rates(
        model, input_tokens, force_long
    )
    cached_tokens = input_tokens * cached_fraction
    cache_write_tokens = input_tokens * cache_write_fraction
    uncached_tokens = input_tokens - cached_tokens - cache_write_tokens
    planned_output_tokens = int(round(base_output_tokens * EFFORTS[effort]["output_factor"]))
    cost = (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + cache_write_tokens * cache_write_rate
        + planned_output_tokens * output_rate
    ) / 1_000_000.0
    return cost, planned_output_tokens, is_long


def _minimum_model_rank(
    stage: str,
    complexity: float,
    ambiguity: float,
    criticality: float,
    coupling: float,
    novelty: float,
) -> int:
    weighted = (
        0.27 * complexity
        + 0.24 * ambiguity
        + 0.22 * criticality
        + 0.15 * coupling
        + 0.12 * novelty
    )
    if weighted >= 4.45:
        return 2
    if stage in {"architecture", "implementation", "review", "integration"}:
        if criticality >= 4.5 and (ambiguity >= 4.0 or coupling >= 4.0):
            return 2
    if weighted >= 2.15 or stage in {"design", "architecture", "implementation", "review", "integration"}:
        return 1
    return 0


def rank_candidates(
    *,
    stage: str,
    complexity: float,
    ambiguity: float,
    criticality: float,
    coupling: float,
    novelty: float,
    determinism: float,
    input_tokens: int,
    base_output_tokens: int,
    cached_fraction: float,
    budget_mode: str,
    force_long: str,
    cache_write_fraction: float = 0.0,
) -> Tuple[float, List[Candidate]]:
    if stage not in STAGE_ADJUSTMENT:
        raise ValueError(f"unknown stage: {stage}")
    for name, value in (
        ("complexity", complexity),
        ("ambiguity", ambiguity),
        ("criticality", criticality),
        ("coupling", coupling),
        ("novelty", novelty),
        ("determinism", determinism),
    ):
        _bounded(name, value)
    if budget_mode not in BUDGET_WEIGHTS:
        raise ValueError(f"unknown budget mode: {budget_mode}")

    difficulty = (
        0.27 * complexity
        + 0.24 * ambiguity
        + 0.22 * criticality
        + 0.15 * coupling
        + 0.12 * novelty
    )
    required_quality = max(1.0, min(5.75, difficulty + STAGE_ADJUSTMENT[stage]))
    minimum_rank = _minimum_model_rank(
        stage, complexity, ambiguity, criticality, coupling, novelty
    )
    weights = BUDGET_WEIGHTS[budget_mode]
    candidates: List[Candidate] = []

    for model_index, model in enumerate(MODEL_ORDER):
        if model_index < minimum_rank:
            continue
        for effort in EFFORT_ORDER:
            # 'none' is only a serious candidate for highly deterministic,
            # tightly scoped work. Retaining it in the catalog keeps costs
            # visible without routing ambiguous work there.
            if effort == "none" and not (
                determinism >= 4.5 and complexity <= 1.75 and ambiguity <= 1.75
            ):
                continue
            model_data = MODELS[model]
            effort_data = EFFORTS[effort]
            affinity = STAGE_AFFINITY.get(model, {}).get(stage, 0.0)
            quality = model_data["capacity"] + effort_data["quality_uplift"] + affinity
            margin = quality - required_quality
            viable = margin >= -0.05
            cost, _, is_long = estimate_cost(
                model,
                effort,
                input_tokens,
                base_output_tokens,
                cached_fraction,
                force_long,
                cache_write_fraction,
            )
            relative_latency = model_data["latency"] * effort_data["latency"]
            cost_pressure = math.log10(1.0 + cost * 1000.0)
            shortfall_penalty = 18.0 * max(0.0, -margin)
            surplus = min(max(margin, 0.0), 1.25)
            score = (
                weights["margin"] * surplus
                - weights["cost"] * cost_pressure
                - weights["latency"] * relative_latency
                - shortfall_penalty
            )
            candidates.append(
                Candidate(
                    model=model,
                    reasoning_effort=effort,
                    estimated_cost_usd=cost,
                    quality_proxy=quality,
                    required_quality=required_quality,
                    quality_margin=margin,
                    relative_latency=relative_latency,
                    score=score,
                    viable=viable,
                    long_context_rates=is_long,
                )
            )

    candidates.sort(key=lambda item: (item.viable, item.score), reverse=True)
    return required_quality, candidates


def _markdown_table(
    input_tokens: int,
    base_output_tokens: int,
    cached_fraction: float,
    cache_write_fraction: float = 0.0,
) -> str:
    lines = [
        "| Model | Effort | Input $/1M | Cached input $/1M | Cache write $/1M | Output $/1M | Planning output factor* | Sample planned output | Sample cost** |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model in MODEL_ORDER:
        data = MODELS[model]
        for effort in EFFORT_ORDER:
            cost, planned_output, _ = estimate_cost(
                model,
                effort,
                input_tokens,
                base_output_tokens,
                cached_fraction,
                "no",
                cache_write_fraction,
            )
            lines.append(
                "| {model} | {effort} | ${input:.2f} | ${cached:.2f} | ${cache_write:.2f} | ${output:.2f} | {factor:.2f}× | {planned:,} | ${cost:.4f} |".format(
                    model=model,
                    effort=effort,
                    input=data["input"],
                    cached=data["cached_input"],
                    cache_write=data["cache_write"],
                    output=data["output"],
                    factor=EFFORTS[effort]["output_factor"],
                    planned=planned_output,
                    cost=cost,
                )
            )
    lines.extend(
        [
            "",
            "*The output factor is a local planning heuristic for expected reasoning/output-token consumption. It is not an OpenAI billing multiplier or quality guarantee.",
            "",
            f"**Sample cost assumes {input_tokens:,} input tokens, {cached_fraction:.0%} cache hits, {cache_write_fraction:.0%} cache writes, and {base_output_tokens:,} base output tokens under short-context standard rates. Actual cost uses measured tokens.",
        ]
    )
    return "\n".join(lines)


def _print_choice(args: argparse.Namespace) -> int:
    required, candidates = rank_candidates(
        stage=args.stage,
        complexity=args.complexity,
        ambiguity=args.ambiguity,
        criticality=args.criticality,
        coupling=args.coupling,
        novelty=args.novelty,
        determinism=args.determinism,
        input_tokens=args.input_tokens,
        base_output_tokens=args.base_output_tokens,
        cached_fraction=args.cached_fraction,
        budget_mode=args.budget_mode,
        force_long=args.long_context,
        cache_write_fraction=args.cache_write_fraction,
    )
    viable = [item for item in candidates if item.viable]
    selected = viable[0] if viable else candidates[0]
    top = candidates[: max(1, args.top)]
    payload = {
        "selected": asdict(selected),
        "required_quality_proxy": required,
        "inputs": {
            "stage": args.stage,
            "complexity": args.complexity,
            "ambiguity": args.ambiguity,
            "criticality": args.criticality,
            "coupling": args.coupling,
            "novelty": args.novelty,
            "determinism": args.determinism,
            "input_tokens": args.input_tokens,
            "base_output_tokens": args.base_output_tokens,
            "cached_fraction": args.cached_fraction,
            "cache_write_fraction": args.cache_write_fraction,
            "budget_mode": args.budget_mode,
            "long_context": args.long_context,
        },
        "alternatives": [asdict(item) for item in top],
        "caveat": (
            "Selection scores and effort output factors are transparent local heuristics. "
            "Token rates are official; the parent coordinator must still inspect current task evidence "
            "and record the final route rationale."
        ),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"selected={selected.model}/{selected.reasoning_effort}")
        print(f"estimated_cost_usd={selected.estimated_cost_usd:.6f}")
        print(f"quality_proxy={selected.quality_proxy:.2f} required={required:.2f}")
        print(f"relative_latency={selected.relative_latency:.2f}")
        print("alternatives:")
        for item in top:
            print(
                f"  {item.model}/{item.reasoning_effort}: "
                f"cost=${item.estimated_cost_usd:.6f}, margin={item.quality_margin:+.2f}, "
                f"score={item.score:.3f}, viable={str(item.viable).lower()}"
            )
        print(payload["caveat"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    table = subparsers.add_parser("table", help="print the model × reasoning planning table")
    table.add_argument("--input-tokens", type=int, default=25_000)
    table.add_argument("--base-output-tokens", type=int, default=2_000)
    table.add_argument("--cached-fraction", type=float, default=0.0)
    table.add_argument("--cache-write-fraction", type=float, default=0.0)
    table.add_argument("--json", action="store_true")

    estimate = subparsers.add_parser("estimate", help="estimate one planned route")
    estimate.add_argument("--model", required=True, choices=MODEL_ORDER)
    estimate.add_argument("--effort", required=True, choices=EFFORT_ORDER)
    estimate.add_argument("--input-tokens", type=int, required=True)
    estimate.add_argument("--base-output-tokens", type=int, required=True)
    estimate.add_argument("--cached-fraction", type=float, default=0.0)
    estimate.add_argument("--cache-write-fraction", type=float, default=0.0)
    estimate.add_argument("--long-context", choices=("auto", "yes", "no"), default="auto")
    estimate.add_argument("--json", action="store_true")

    choose = subparsers.add_parser("choose", help="rank live model/effort candidates")
    choose.add_argument("--stage", required=True, choices=tuple(STAGE_ADJUSTMENT))
    choose.add_argument("--complexity", type=float, required=True)
    choose.add_argument("--ambiguity", type=float, required=True)
    choose.add_argument("--criticality", type=float, required=True)
    choose.add_argument("--coupling", type=float, required=True)
    choose.add_argument("--novelty", type=float, required=True)
    choose.add_argument("--determinism", type=float, required=True)
    choose.add_argument("--input-tokens", type=int, default=25_000)
    choose.add_argument("--base-output-tokens", type=int, default=2_000)
    choose.add_argument("--cached-fraction", type=float, default=0.0)
    choose.add_argument("--cache-write-fraction", type=float, default=0.0)
    choose.add_argument("--budget-mode", choices=tuple(BUDGET_WEIGHTS), default="balanced")
    choose.add_argument("--long-context", choices=("auto", "yes", "no"), default="auto")
    choose.add_argument("--top", type=int, default=3)
    choose.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "table":
            if args.json:
                rows = []
                for model in MODEL_ORDER:
                    for effort in EFFORT_ORDER:
                        cost, planned_output, _ = estimate_cost(
                            model,
                            effort,
                            args.input_tokens,
                            args.base_output_tokens,
                            args.cached_fraction,
                            "no",
                            args.cache_write_fraction,
                        )
                        rows.append(
                            {
                                "model": model,
                                "reasoning_effort": effort,
                                "input_usd_per_million": MODELS[model]["input"],
                                "cached_input_usd_per_million": MODELS[model]["cached_input"],
                                "cache_write_usd_per_million": MODELS[model]["cache_write"],
                                "output_usd_per_million": MODELS[model]["output"],
                                "planning_output_factor": EFFORTS[effort]["output_factor"],
                                "sample_planned_output_tokens": planned_output,
                                "sample_cost_usd": cost,
                            }
                        )
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                print(
                    _markdown_table(
                        args.input_tokens,
                        args.base_output_tokens,
                        args.cached_fraction,
                        args.cache_write_fraction,
                    )
                )
            return 0
        if args.command == "estimate":
            cost, planned_output, is_long = estimate_cost(
                args.model,
                args.effort,
                args.input_tokens,
                args.base_output_tokens,
                args.cached_fraction,
                args.long_context,
                args.cache_write_fraction,
            )
            result = {
                "model": args.model,
                "reasoning_effort": args.effort,
                "estimated_cost_usd": cost,
                "planned_output_tokens": planned_output,
                "long_context_rates": is_long,
                "caveat": "The effort output factor is a planning heuristic, not a billing multiplier.",
            }
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                for key, value in result.items():
                    print(f"{key}={value}")
            return 0
        if args.command == "choose":
            return _print_choice(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
