# prompt-lab

## Goal

- Measure how well `claude-haiku-5-5` triages synthetic SRE alerts with one fixed prompt.
- Test whether a severity rubric in the prompt improves severity accuracy.
- Model sees only alert text and returns only a JSON object: `service`, `severity`, `cause_category`, `next_step`.

## Setup

- `alerts.jsonl`: 22 synthetic alerts with fake names.
  - `alert-001` to `alert-020`: Prometheus alerts, Kubernetes events, app error logs; some noisy (007, 010, 013, 016).
  - `alert-021`, `alert-022`: deliberately nonsense; labels all `unknown`.
- `run_baseline.py`:
  - One API call per alert, `max_tokens=512`.
  - Strict `json.loads`; markdown fences count as invalid. `max_tokens` stop reason is an error.
  - Exact-match scoring for `service`, `severity`, `cause_category`. `next_step` is printed beside the model output for human review.
  - Cost: $0.10 per million input tokens, $0.50 per million output tokens.
- Run: `cd llm-hello && ANTHROPIC_API_KEY=... uv run python ../prompt-lab/run_baseline.py`
- Limitation: result files do not record the prompt version. Run 1 predates alert-021/022 (20 alerts). Runs 6-8 are identified as the rubric runs by input size (339 input tokens per call vs 258-263 in runs 1-5).

## Results

- Runs 1-5: baseline (no rubric). Runs 6-8: rubric prompt.
- Aggregates use the four 22-alert baseline runs (2-5, 88 alert-calls) and the three rubric runs (6-8, 66 alert-calls).

| Metric | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Run 6 (rubric) | Run 7 (rubric) | Run 8 (rubric) |
|---|---|---|---|---|---|---|---|---|
| Alerts | 20 | 22 | 22 | 22 | 22 | 22 | 22 | 22 |
| service | 19/20 | 20/22 | 19/22 | 19/22 | 18/22 | 20/22 | 18/22 | 20/22 |
| severity | 12/20 | 12/22 | 13/22 | 15/22 | 15/22 | 13/22 | 11/22 | 14/22 |
| cause_category | 16/20 | 17/22 | 19/22 | 19/22 | 20/22 | 19/22 | 16/22 | 19/22 |
| sev ±1 | 20/20 | 20/22 | 19/22 | 20/22 | 19/22 | 19/22 | 18/22 | 20/22 |
| max_tokens truncations | 0 | 0 | 1 (alert-009) | 0 | 1 (alert-008) | 1 (alert-016) | 2 (alert-017, alert-020) | 0 |
| invalid JSON | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Cost (USD) | 0.002641 | 0.002574 | 0.003423 | 0.003381 | 0.003346 | 0.004368 | 0.004422 | 0.004118 |

| Aggregate | Baseline (runs 2-5) | Rubric (runs 6-8) |
|---|---|---|
| service | 76/88 (86.4%) | 58/66 (87.9%) |
| severity | 55/88 (62.5%) | 38/66 (57.6%) |
| cause_category | 75/88 (85.2%) | 54/66 (81.8%) |
| Average cost per run (USD) | 0.003181 | 0.004303 |
| max_tokens truncations | 2 | 3 |

- Scored against current labels. Failed or truncated calls count as misses.
- `sev ±1` = severity within one level.
- Timestamps: 20261030T150347Z, T154030Z, T155805Z, T155945Z, T160418Z, T180327Z, T180519Z, T180659Z (runs 1-8).
- alert-016's service label is `blackbox-exporter`, but its alert text says `job=blackbox-staging`; treat that label as suspect.
- Rubric:
  - critical: customer-facing outage or data loss happening now
  - high: degraded user impact or an imminent outage
  - medium: no user impact yet, needs action within hours
  - low: informational or noise
  - unknown: not enough information

## Lessons

1. Rubric lifted exact severity from 14.0 to 16.0 out of 22 per run, but the ranges touch (15 vs 15). Not conclusive at n=5.
2. About two-thirds of the gain comes from two alerts whose labels were rewritten from the rubric after seeing results. Without them the gain is about +0.6 per run. Labels written from the rubric favor the rubric by design.
3. The rubric fixes under-calling of clear cases (002-004) and costs accuracy on ambiguous low alerts (010, 013), and within-one fell. Only one critical alert exists, so critical is effectively untested.
