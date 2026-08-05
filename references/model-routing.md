# Model and reasoning ROI table

Research baseline: **2026-08-05**. Official rates come from the OpenAI GPT-5.6 model and pricing documentation. The sample cost column combines those official rates with a transparent local estimate of reasoning/output-token volume.

OpenAI prices token categories, not the reasoning-effort label itself. There is no published fixed `low`, `high`, or `max` multiplier. The planning output factors below are deliberately labeled heuristics and may be tuned from measured workflow telemetry without changing the official rates.

## Model × effort planning table

| Model | Effort | Input $/1M | Cached input $/1M | Cache write $/1M | Output $/1M | Planning output factor* | Sample planned output | Sample cost** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-5.6-luna | none | $0.20 | $0.02 | $0.25 | $1.20 | 0.55× | 1,100 | $0.0063 |
| gpt-5.6-luna | low | $0.20 | $0.02 | $0.25 | $1.20 | 0.75× | 1,500 | $0.0068 |
| gpt-5.6-luna | medium | $0.20 | $0.02 | $0.25 | $1.20 | 1.00× | 2,000 | $0.0074 |
| gpt-5.6-luna | high | $0.20 | $0.02 | $0.25 | $1.20 | 1.40× | 2,800 | $0.0084 |
| gpt-5.6-luna | xhigh | $0.20 | $0.02 | $0.25 | $1.20 | 1.90× | 3,800 | $0.0096 |
| gpt-5.6-luna | max | $0.20 | $0.02 | $0.25 | $1.20 | 2.60× | 5,200 | $0.0112 |
| gpt-5.6-terra | none | $2.00 | $0.20 | $2.50 | $12.00 | 0.55× | 1,100 | $0.0632 |
| gpt-5.6-terra | low | $2.00 | $0.20 | $2.50 | $12.00 | 0.75× | 1,500 | $0.0680 |
| gpt-5.6-terra | medium | $2.00 | $0.20 | $2.50 | $12.00 | 1.00× | 2,000 | $0.0740 |
| gpt-5.6-terra | high | $2.00 | $0.20 | $2.50 | $12.00 | 1.40× | 2,800 | $0.0836 |
| gpt-5.6-terra | xhigh | $2.00 | $0.20 | $2.50 | $12.00 | 1.90× | 3,800 | $0.0956 |
| gpt-5.6-terra | max | $2.00 | $0.20 | $2.50 | $12.00 | 2.60× | 5,200 | $0.1124 |
| gpt-5.6-sol | none | $5.00 | $0.50 | $6.25 | $30.00 | 0.55× | 1,100 | $0.1580 |
| gpt-5.6-sol | low | $5.00 | $0.50 | $6.25 | $30.00 | 0.75× | 1,500 | $0.1700 |
| gpt-5.6-sol | medium | $5.00 | $0.50 | $6.25 | $30.00 | 1.00× | 2,000 | $0.1850 |
| gpt-5.6-sol | high | $5.00 | $0.50 | $6.25 | $30.00 | 1.40× | 2,800 | $0.2090 |
| gpt-5.6-sol | xhigh | $5.00 | $0.50 | $6.25 | $30.00 | 1.90× | 3,800 | $0.2390 |
| gpt-5.6-sol | max | $5.00 | $0.50 | $6.25 | $30.00 | 2.60× | 5,200 | $0.2810 |

*The output factor is a local planning heuristic for expected reasoning/output-token consumption. It is not an OpenAI billing multiplier or quality guarantee.

**Sample cost assumes 25,000 input tokens, 0% cache hits, 0% cache writes, and 2,000 base output tokens under short-context standard rates. Actual cost uses measured tokens.

## Long-context rates

When input exceeds 272,000 tokens, use these published standard rates:

| Model | Input $/1M | Cached input $/1M | Cache write $/1M | Output $/1M |
|---|---:|---:|---:|---:|
| `gpt-5.6-luna` | $0.40 | $0.04 | $0.50 | $1.80 |
| `gpt-5.6-terra` | $4.00 | $0.40 | $5.00 | $18.00 |
| `gpt-5.6-sol` | $10.00 | $1.00 | $12.50 | $45.00 |

The helper switches to these rates automatically when planned input exceeds 272,000 tokens unless explicitly overridden.

## Cost formula

For a planning estimate, divide input tokens into mutually exclusive ordinary-input, cache-hit, and cache-write categories:

```text
planned_output = base_output × effort_output_factor
ordinary_input = total_input - cache_hit_input - cache_write_input
cost = (
  ordinary_input × input_rate
  + cache_hit_input × cached_input_rate
  + cache_write_input × cache_write_rate
  + planned_output × output_rate
) / 1,000,000
```

`--cached-fraction` and `--cache-write-fraction` must each be between zero and one and may not sum above one. Replace planned tokens with measured tokens when actual usage is available. Codex subscription quotas or credits may not equal API dollar billing, so use the dollar estimate as a consistent ROI proxy when direct usage telemetry is unavailable.

## Effort planning factors

| Effort | Local output factor | Intended use |
|---|---:|---|
| `none` | 0.55× | Fully deterministic, tiny transformations or checks. |
| `low` | 0.75× | Narrow, well-specified work with little ambiguity. |
| `medium` | 1.00× | Balanced default for ordinary bounded work. |
| `high` | 1.40× | Difficult synthesis, diagnosis, coding, or review. |
| `xhigh` | 1.90× | Deep cross-file or cross-source reasoning with material ambiguity. |
| `max` | 2.60× | Hardest quality-first work where added reasoning has high expected value. |

These factors estimate token consumption only. They are not OpenAI billing multipliers or quality guarantees. The model router separately uses quality and relative-latency proxies.

## Runtime routing inputs

Score each node from 1 to 5:

- **Complexity:** number and depth of reasoning steps.
- **Ambiguity:** uncertainty in intent, evidence, or solution path.
- **Criticality:** cost of a wrong result or failed integration.
- **Coupling:** number of contracts, modules, or stakeholders affected.
- **Novelty:** distance from repository patterns and common solutions.
- **Determinism:** how mechanically the result can be checked; 5 means highly deterministic.

Also estimate input tokens, base output/reasoning tokens, cache-hit fraction, cache-write fraction, and whether the work favors value, balance, or quality.

Run:

```sh
python3 scripts/model_router.py choose \
  --stage architecture \
  --complexity 5 --ambiguity 4 --criticality 5 \
  --coupling 4 --novelty 4 --determinism 2 \
  --input-tokens 60000 --base-output-tokens 5000 \
  --cached-fraction 0.20 --cache-write-fraction 0.05 \
  --budget-mode quality --top 4 --json
```

When calling from the skill, use the absolute skill script path shown in `SKILL.md`.

## Decision policy

1. Treat the helper as a ranked recommendation, not an oracle.
2. Select the cheapest viable route unless current evidence shows a more capable route has better expected value.
3. Record why the chosen route beats the closest cheaper alternative.
4. Re-route every retry from current conditions.
5. Escalate for capability failure; improve packet, scope, dependencies, or evidence for orchestration failure.
6. Downgrade mechanical follow-up work even if the producer used a more expensive route.
7. Use Sol/max children sparingly. The parent is already Sol/max and can integrate, triage, and repair the graph.

## Practical priors

| Work shape | Typical starting route | Reconsider when |
|---|---|---|
| Source enumeration or deterministic check | Luna low/medium | Synthesis or ambiguity emerges. |
| Bounded factual synthesis | Terra medium/high | Sources conflict materially or consequences are high. |
| Routine documentation from stable behavior | Luna medium or Terra low | Behavior is unclear or cross-cutting. |
| Substantial implementation | Terra high | Work is either mechanical enough for Luna or novel/cross-cutting enough for Sol. |
| Architecture or hard diagnosis | Terra xhigh | Criticality and ambiguity jointly require Sol. |
| Independent final review | Terra high/xhigh | Wide, high-impact changes or repeated misses justify Sol. |
| Narrow accepted-finding fix | Luna medium or Terra medium | Root cause is deeper than the finding suggests. |

Never encode these priors in an agent TOML. Runtime evidence must remain able to change every route.
