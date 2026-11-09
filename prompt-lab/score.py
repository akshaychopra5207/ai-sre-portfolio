import hashlib
import json
import sys
from fractions import Fraction
from pathlib import Path
from statistics import mean

HERE = Path(__file__).parent
ALERTS_PATH = HERE / "alerts.jsonl"
LEVELS = ["low", "medium", "high", "critical"]  # ordered; "unknown" is not in here on purpose


def within_one(model, label):
    # unknown (or anything off-scale) never counts as within one level
    if model not in LEVELS or label not in LEVELS:
        return False
    return abs(LEVELS.index(model) - LEVELS.index(label)) <= 1


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    labels_hash = hashlib.sha256(ALERTS_PATH.read_bytes()).hexdigest()
    labels = {a["id"]: a["labels"]["severity"] for a in load(ALERTS_PATH)}
    files = sorted(HERE.glob("results/*_2026*.jsonl"))
    if not files:
        sys.exit("no result files match results/*_2026*.jsonl")
    runs = {f: load(f) for f in files}

    # 1. refuse to score if any result line was produced against different labels
    bad = [f.name for f, rows in runs.items() if any(r.get("labels_sha256") != labels_hash for r in rows)]
    if bad:
        print(f"ERROR: labels_sha256 differs from alerts.jsonl ({labels_hash[:12]}...) in:")
        for name in bad:
            print(f"  {name}")
        sys.exit(1)

    # 2. score each run; failed or missing alerts count as misses, denominator is all labelled alerts
    n = len(labels)
    by_variant = {}  # variant -> list of (file name, exact hits, within-one hits, {alert id: exact hit})
    for f, rows in runs.items():
        variants = {r["variant"] for r in rows}
        if len(variants) != 1:
            sys.exit(f"ERROR: {f.name} has mixed or missing variants: {variants}")
        if len(rows) != n:
            print(f"WARNING: {f.name} has {len(rows)} alerts, expected {n}; missing ones count as misses")
        model_sev = {r["id"]: (r["parsed"] or {}).get("severity") for r in rows}
        exact = {i: model_sev.get(i) == lab for i, lab in labels.items()}
        near = sum(within_one(model_sev.get(i), lab) for i, lab in labels.items())
        by_variant.setdefault(variants.pop(), []).append((f.name, sum(exact.values()), near, exact))

    # 3. per-variant table: hits per run, mean, min-max
    for variant, runs_v in sorted(by_variant.items()):
        print(f"\nvariant: {variant} ({len(runs_v)} runs)")
        print(f"  {'run':<40}{'exact':>8}{'within-1':>10}")
        for name, ex, nr, _ in runs_v:
            print(f"  {name:<40}{f'{ex}/{n}':>8}{f'{nr}/{n}':>10}")
        for label, fn in (("mean", lambda v: f"{mean(v):.1f}"), ("min-max", lambda v: f"{min(v)}-{max(v)}")):
            print(f"  {label:<40}{fn([r[1] for r in runs_v]):>8}{fn([r[2] for r in runs_v]):>10}")

    # 4. alerts whose exact-hit rate differs between variants
    names = sorted(by_variant)
    print(f"\nalerts where exact severity hit rate differs between variants")
    print(f"  {'alert':<12}{'label':<10}" + "".join(f"{v + ' hits/' + str(len(by_variant[v])):>16}" for v in names))
    shown = 0
    for alert_id, label in labels.items():
        hits = {v: sum(r[3][alert_id] for r in by_variant[v]) for v in names}
        rates = {Fraction(hits[v], len(by_variant[v])) for v in names}
        if len(rates) > 1:
            shown += 1
            print(f"  {alert_id:<12}{label:<10}" + "".join(f"{hits[v]:>16}" for v in names))
    if not shown:
        print("  none")


if __name__ == "__main__":
    main()
