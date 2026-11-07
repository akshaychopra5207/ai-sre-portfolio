import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 512  # a 4-field JSON answer is ~100 tokens; this leaves ample headroom

# Haiku 5.5 pricing, USD per million tokens
PRICE_INPUT_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 0.50

HERE = Path(__file__).parent
ALERTS_PATH = HERE / "alerts.jsonl"
RESULTS_DIR = HERE / "results"

SEVERITY_LEVELS = ["low", "medium", "high", "critical"]  # ordered, least to most severe
OTHER = "other"  # model severity outside SEVERITY_LEVELS
UNKNOWN = "unknown"

SCORED_FIELDS = ["service", "severity", "cause_category"]
ALL_FIELDS = SCORED_FIELDS + ["next_step"]

_PROMPT_HEAD = """You are an SRE triaging an incoming alert.

Respond with ONLY a JSON object, no markdown fences and no other text, with exactly these keys:
- "service": the name of the affected service, string
- "severity": one of "low", "medium", "high", "critical", "unknown"
- "cause_category": one of "resource_exhaustion", "dependency_failure", "bad_deploy", "config_error", "network", "capacity", "unknown"
- "next_step": the single most useful next action for the on-call engineer, one sentence

"""

_SEVERITY_RUBRIC_BLOCK = """Severity rubric:
critical: customer-facing outage or data loss happening now
high: degraded user impact or an imminent outage
medium: no user impact yet, needs action within hours
low: informational or noise
unknown: not enough information

"""

_PROMPT_TAIL = """Alert:
{alert_text}"""

# The two variants differ only by the severity rubric block.
PROMPT_BASELINE = _PROMPT_HEAD + _PROMPT_TAIL
PROMPT_RUBRIC = _PROMPT_HEAD + _SEVERITY_RUBRIC_BLOCK + _PROMPT_TAIL
PROMPTS = {"baseline": PROMPT_BASELINE, "rubric": PROMPT_RUBRIC}


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def load_alerts(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def cost_usd(input_tokens, output_tokens):
    return (
        input_tokens * PRICE_INPUT_PER_MTOK + output_tokens * PRICE_OUTPUT_PER_MTOK
    ) / 1e6


def severity_within_one(model_sev, label_sev):
    if model_sev not in SEVERITY_LEVELS or label_sev not in SEVERITY_LEVELS:
        return False
    return abs(SEVERITY_LEVELS.index(model_sev) - SEVERITY_LEVELS.index(label_sev)) <= 1


def print_severity_matrix(scored):
    cols = SEVERITY_LEVELS + [OTHER]
    rows = SEVERITY_LEVELS + [UNKNOWN]  # labels may be "unknown" for uninformative alerts
    counts = {row: {col: 0 for col in cols} for row in rows}
    for r in scored:
        model_sev = r["parsed"]["severity"]
        counts[r["label"]["severity"]][model_sev if model_sev in SEVERITY_LEVELS else OTHER] += 1

    width = 9
    print("SEVERITY CONFUSION MATRIX (rows = label, columns = model; scored alerts only)")
    print("  " + "label \\ model".ljust(14) + "".join(c.rjust(width) for c in cols))
    for row in rows:
        print("  " + row.ljust(14) + "".join(str(counts[row][c]).rjust(width) for c in cols))


def parse_output(raw):
    """Return (parsed, error). parsed is a dict with all four fields or None."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"unparseable JSON: {e}"
    if not isinstance(parsed, dict):
        return None, f"expected JSON object, got {type(parsed).__name__}"
    missing = [k for k in ALL_FIELDS if k not in parsed]
    if missing:
        return None, f"missing fields: {missing}"
    return parsed, None


def classify_alert(client, alert, prompt, meta):
    result = {
        "id": alert["id"],
        **meta,
        "raw_output": None,
        "parsed": None,
        "label": alert["labels"],
        "match": {f: False for f in SCORED_FIELDS},
        "severity_within_one": False,
        "stop_reason": None,
        "flagged_stop_reason": True,  # True unless stop_reason is "end_turn"
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": prompt.format(alert_text=alert["text"]),
                }
            ],
        )
    except anthropic.APIError as e:
        result["error"] = f"api_error: {e}"
        return result

    result["raw_output"] = "".join(b.text for b in resp.content if b.type == "text")
    result["stop_reason"] = resp.stop_reason
    result["flagged_stop_reason"] = resp.stop_reason != "end_turn"
    result["input_tokens"] = resp.usage.input_tokens
    result["output_tokens"] = resp.usage.output_tokens
    result["cost_usd"] = cost_usd(resp.usage.input_tokens, resp.usage.output_tokens)

    if resp.stop_reason == "max_tokens":
        result["error"] = f"max_tokens: output truncated at {MAX_TOKENS} tokens"
        return result

    parsed, err = parse_output(result["raw_output"])
    if err:
        result["error"] = f"invalid_json: {err}"
        return result

    result["parsed"] = parsed
    result["match"] = {f: parsed[f] == alert["labels"][f] for f in SCORED_FIELDS}
    result["severity_within_one"] = severity_within_one(
        parsed["severity"], alert["labels"]["severity"]
    )
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="Run the alert triage eval.")
    parser.add_argument(
        "--variant",
        required=True,
        choices=sorted(PROMPTS),
        help="which prompt to run; no default, so the wrong one cannot run by accident",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the prompt and hashes, then exit without calling the API",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    prompt = PROMPTS[args.variant]
    meta = {
        "variant": args.variant,
        "prompt_sha256": sha256_hex(prompt.encode()),
        "labels_sha256": sha256_hex(ALERTS_PATH.read_bytes()),
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
    }
    alerts = load_alerts(ALERTS_PATH)

    print(f"Variant: {args.variant}  model: {MODEL}  max_tokens: {MAX_TOKENS}  alerts: {len(alerts)}")
    print(f"prompt_sha256: {meta['prompt_sha256']}")
    print(f"labels_sha256: {meta['labels_sha256']}")
    print("=" * 72)
    print("PROMPT (identical for every alert; {alert_text} is replaced per alert):")
    print("-" * 72)
    print(prompt)
    print("=" * 72)

    if args.dry_run:
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set in the environment")

    client = anthropic.Anthropic()

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"{args.variant}_{stamp}.jsonl"

    results = []
    with open(out_path, "w") as out:
        for alert in alerts:
            r = classify_alert(client, alert, prompt, meta)
            results.append(r)
            out.write(json.dumps(r) + "\n")
            out.flush()

            if r["flagged_stop_reason"]:
                print(f"[{r['id']}] FLAG stop_reason={r['stop_reason']!r} (expected 'end_turn')")
            if r["error"]:
                print(f"[{r['id']}] ERROR {r['error']}")
                if r["raw_output"] is not None:
                    print(f"    raw: {r['raw_output']!r}")
                continue
            flags = " ".join(
                f"{f}={'ok' if r['match'][f] else 'MISS'}" for f in SCORED_FIELDS
            )
            print(f"[{r['id']}] {flags}")
            print(f"    model next_step: {r['parsed']['next_step']}")
            print(f"    label next_step: {r['label']['next_step']}")

    n = len(results)
    scored = [r for r in results if r["parsed"] is not None]
    invalid = [r for r in results if r["error"] and r["error"].startswith("invalid_json")]
    truncated = [r for r in results if r["error"] and r["error"].startswith("max_tokens")]
    api_errors = [r for r in results if r["error"] and r["error"].startswith("api_error")]
    total_in = sum(r["input_tokens"] for r in results)
    total_out = sum(r["output_tokens"] for r in results)

    print("=" * 72)
    print("SUMMARY (accuracy denominator = all alerts; failed calls count as misses)")
    for f in SCORED_FIELDS:
        hits = sum(r["match"][f] for r in results)
        print(f"  {f:<16} {hits}/{n} = {hits / n:.1%}")
    within = sum(r["severity_within_one"] for r in results)
    print(f"  severity ±1 level {within}/{n} = {within / n:.1%}")
    print(f"  invalid JSON:     {len(invalid)}")
    print(f"  max_tokens:       {len(truncated)}")
    print(f"  API errors:       {len(api_errors)}")
    print(f"  tokens:           {total_in} in, {total_out} out")
    print(f"  total cost:       ${cost_usd(total_in, total_out):.6f}")

    print_severity_matrix(scored)

    print("MISMATCHES")
    any_mismatch = False
    for r in scored:
        for f in SCORED_FIELDS:
            if not r["match"][f]:
                any_mismatch = True
                print(
                    f"  [{r['id']}] {f}: model={r['parsed'][f]!r} label={r['label'][f]!r}"
                )
    if not any_mismatch:
        print("  none")
    for r in results:
        if r["error"]:
            print(f"  [{r['id']}] no score: {r['error']}")

    print(f"Results written to {out_path}")


if __name__ == "__main__":
    main()
