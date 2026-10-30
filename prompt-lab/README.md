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

1. **The rubric did not improve severity accuracy:** 38/66 (57.6%) exact vs 55/88 (62.5%) for baseline. Per-run severity ranges overlap (rubric 11-14/22, baseline 12-15/22), and the rubric costs more ($0.004303 vs $0.003181 average per run).
2. **The rubric moved errors around instead of removing them:** severity hits on alert-002 went 0/4 to 2/3, alert-003 1/4 to 3/3, alert-004 0/4 to 3/3. It got worse on the noisy and ambiguous alerts: alert-007 4/4 to 0/3, alert-010 4/4 to 1/3, alert-009 2/4 to 0/3, alert-013 1/4 to 0/3. alert-012 (critical) stayed `high` in all 7 runs from 2 on.
3. **Nonsense alerts, no invented causes:** cause_category was `unknown` in all 14 outputs (runs 2-8). Without an `unknown` severity option (run 2) the model picked `medium` for both; in runs 3-8 it answered `unknown` in all 12 outputs. alert-021's service was echoed as `xk_check_7` in runs 3-5 (a miss against label `unknown`) but was `unknown` in all three rubric runs.
