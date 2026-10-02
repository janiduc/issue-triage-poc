"""Compare the LLM approach with the conventional ML baseline on a labelled test set.

Usage:
    python evaluate.py                      # baseline only (no API key needed)
    ANTHROPIC_API_KEY=... python evaluate.py  # baseline + LLM

Optional, to estimate cost from measured token usage (check the provider's current pricing page):
    PRICE_IN_PER_MTOK=1.00 PRICE_OUT_PER_MTOK=5.00 ISSUES_PER_MONTH=400 python evaluate.py

Outputs a summary table and writes evaluation_results.csv for your report.
"""
import csv
import os
import time

from sklearn.metrics import accuracy_score, f1_score

from triage import config
from triage.baseline import BaselineClassifier
from triage.llm import LLMError, triage_with_llm
from triage.similarity import SimilarityIndex

FIELDS = ["type", "module", "priority"]


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    seed = load_csv(config.SEED_CSV)
    tests = load_csv(config.EVAL_CSV)
    train_rows = [{"title": r["title"], "description": r["description"],
                   "final_type": r["type"], "final_module": r["module"],
                   "final_priority": r["priority"]} for r in seed]
    baseline = BaselineClassifier().fit(train_rows)
    kb = [{"id": i + 1, "title": r["title"], "description": r["description"],
           "resolution": r["resolution"]} for i, r in enumerate(seed)]
    index = SimilarityIndex().build(kb)

    run_llm = config.llm_enabled()
    preds = {"baseline": {f: [] for f in FIELDS}, "llm": {f: [] for f in FIELDS}}
    latency = {"baseline": [], "llm": []}
    tokens_in, tokens_out, llm_failures = [], [], 0
    rows_out = []

    for r in tests:
        text = f"{r['title']}. {r['description']}"
        t0 = time.perf_counter()
        b = baseline.predict(text)
        latency["baseline"].append((time.perf_counter() - t0) * 1000)
        for f in FIELDS:
            preds["baseline"][f].append(b[f])
        out = {"title": r["title"], **{f"true_{f}": r[f] for f in FIELDS},
               **{f"baseline_{f}": b[f] for f in FIELDS}}

        if run_llm:
            similar = index.search(text)
            t0 = time.perf_counter()
            try:
                res, usage = triage_with_llm(r["title"], r["description"], similar)
                tokens_in.append(usage["input_tokens"])
                tokens_out.append(usage["output_tokens"])
            except LLMError as exc:
                print(f"  LLM failed on '{r['title']}': {exc}")
                llm_failures += 1
                res = {f: "error" for f in FIELDS}
            latency["llm"].append((time.perf_counter() - t0) * 1000)
            for f in FIELDS:
                preds["llm"][f].append(res[f])
                out[f"llm_{f}"] = res[f]
        rows_out.append(out)

    print(f"\nTest set: {len(tests)} issues | similarity method: {index.method}\n")
    print(f"{'Approach':<10}{'Field':<10}{'Accuracy':>10}{'Macro-F1':>10}")
    for approach in (["baseline", "llm"] if run_llm else ["baseline"]):
        for f in FIELDS:
            y_true = [r[f] for r in tests]
            y_pred = preds[approach][f]
            acc = accuracy_score(y_true, y_pred)
            f1 = f1_score(y_true, y_pred, average="macro", zero_division=0)
            print(f"{approach:<10}{f:<10}{acc:>10.2f}{f1:>10.2f}")
        avg_ms = sum(latency[approach]) / len(latency[approach])
        print(f"{approach:<10}{'latency':<10}{avg_ms:>9.0f} ms average per issue\n")

    if run_llm and tokens_in:
        avg_in, avg_out = sum(tokens_in) / len(tokens_in), sum(tokens_out) / len(tokens_out)
        print(f"LLM failures: {llm_failures} | avg tokens per issue: {avg_in:.0f} in, {avg_out:.0f} out")
        p_in, p_out = os.getenv("PRICE_IN_PER_MTOK"), os.getenv("PRICE_OUT_PER_MTOK")
        if p_in and p_out:
            per_issue = avg_in / 1e6 * float(p_in) + avg_out / 1e6 * float(p_out)
            monthly = per_issue * int(os.getenv("ISSUES_PER_MONTH", "400"))
            print(f"Estimated cost: {per_issue:.5f} per issue, {monthly:.2f} per month "
                  f"(same currency as the prices you entered)")
    elif not run_llm:
        print("LLM not evaluated: set ANTHROPIC_API_KEY to include it in the comparison.")

    with open("evaluation_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows_out[0].keys())
        writer.writeheader()
        writer.writerows(rows_out)
    print("Per-issue results written to evaluation_results.csv")


if __name__ == "__main__":
    main()
