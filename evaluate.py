"""Compare the LLM approach with the conventional ML baseline on a labelled test set.

Usage:
    python evaluate.py                          # baseline only (no model needed)
    python evaluate.py                          # + LLM, if a model is configured in .env:
                                                #   LLM_PROVIDER=ollama (free, local) or ANTHROPIC_API_KEY
    python evaluate.py --limit 10               # first 10 issues only (quick check of a local model)

Optional, to estimate cost from measured token usage (check the provider's current pricing page;
a local Ollama model costs nothing):
    PRICE_IN_PER_MTOK=1.00 PRICE_OUT_PER_MTOK=5.00 ISSUES_PER_MONTH=400 python evaluate.py

Writes evaluation_results.csv (one row per issue) and evaluation_report.txt (the printed summary
plus a confusion matrix per field) for the report.

The test set (data/eval_issues.csv) marks each issue's case: normal, duplicate (of a seeded past
issue), vague, or injection (text that tries to change the classification).
"""
import argparse
import csv
import io
import os
import time
from contextlib import redirect_stdout

from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

from triage import config
from triage.baseline import BaselineClassifier
from triage.llm import LLMError, triage_with_llm
from triage.similarity import SimilarityIndex

FIELDS = ["type", "module", "priority"]
LABELS = {"type": config.ISSUE_TYPES, "module": config.MODULES, "priority": config.PRIORITIES}


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def confusion(field, y_true, y_pred):
    labels = LABELS[field]
    m = confusion_matrix(y_true, y_pred, labels=labels)
    width = max(len(l) for l in labels) + 2
    lines = [f"  {field}: rows = true label, columns = predicted",
             " " * (width + 2) + "".join(l[:6].rjust(8) for l in labels)]
    for label, row in zip(labels, m):
        lines.append("  " + label.ljust(width) + "".join(str(n).rjust(8) for n in row))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="evaluate only the first N issues")
    args = parser.parse_args()
    config.load_env_file()

    seed = load_csv(config.SEED_CSV)
    tests = load_csv(config.EVAL_CSV)[:args.limit]
    train_rows = [{"title": r["title"], "description": r["description"],
                   "final_type": r["type"], "final_module": r["module"],
                   "final_priority": r["priority"]} for r in seed]
    baseline = BaselineClassifier().fit(train_rows)
    kb = [{"id": i + 1, "title": r["title"], "description": r["description"],
           "resolution": r["resolution"]} for i, r in enumerate(seed)]
    index = SimilarityIndex().build(kb)

    run_llm = config.llm_enabled()
    approaches = ["baseline", "llm"] if run_llm else ["baseline"]
    preds = {a: {f: [] for f in FIELDS} for a in approaches}
    dup_pred = {a: [] for a in approaches}
    latency = {a: [] for a in approaches}
    tokens_in, tokens_out, llm_failures = [], [], 0
    rows_out = []

    for n, r in enumerate(tests, 1):
        text = f"{r['title']}. {r['description']}"
        similar = index.search(text)
        top = similar[0] if similar else None
        t0 = time.perf_counter()
        b = baseline.predict(text)
        latency["baseline"].append((time.perf_counter() - t0) * 1000)
        # Baseline duplicate rule, as used in the app's fallback mode.
        b_dup = top["id"] if top and top["similarity"] >= index.duplicate_threshold else None
        for f in FIELDS:
            preds["baseline"][f].append(b[f])
        dup_pred["baseline"].append(b_dup)
        out = {"title": r["title"], "case": r.get("case", "normal"),
               "true_duplicate_of": r.get("duplicate_of_seed") or "",
               **{f"true_{f}": r[f] for f in FIELDS},
               **{f"baseline_{f}": b[f] for f in FIELDS}, "baseline_duplicate_of": b_dup or "",
               "top_similarity": top["similarity"] if top else ""}

        if run_llm:
            print(f"  [{n}/{len(tests)}] {r['title'][:60]}", flush=True)
            t0 = time.perf_counter()
            try:
                res, usage = triage_with_llm(r["title"], r["description"], similar)
                tokens_in.append(usage["input_tokens"])
                tokens_out.append(usage["output_tokens"])
            except LLMError as exc:
                print(f"    LLM failed: {exc}")
                llm_failures += 1
                res = {f: "error" for f in FIELDS} | {"duplicate_of": None, "confidence": None}
            latency["llm"].append((time.perf_counter() - t0) * 1000)
            for f in FIELDS:
                preds["llm"][f].append(res[f])
                out[f"llm_{f}"] = res[f]
            dup_pred["llm"].append(res.get("duplicate_of"))
            out["llm_duplicate_of"] = res.get("duplicate_of") or ""
            out["llm_confidence"] = res.get("confidence")
        rows_out.append(out)

    report = io.StringIO()
    with redirect_stdout(report):
        model = (f"{config.llm_model()} via {config.llm_provider()}" if run_llm
                 else "not evaluated (configure LLM_PROVIDER=ollama or ANTHROPIC_API_KEY)")
        print(f"Test set: {len(tests)} issues | similarity: {index.method} | LLM: {model}\n")
        print(f"{'Approach':<10}{'Field':<10}{'Accuracy':>10}{'Macro-F1':>10}")
        for approach in approaches:
            for f in FIELDS:
                y_true = [r[f] for r in tests]
                y_pred = preds[approach][f]
                acc = accuracy_score(y_true, y_pred)
                f1 = f1_score(y_true, y_pred, labels=LABELS[f], average="macro", zero_division=0)
                print(f"{approach:<10}{f:<10}{acc:>10.2f}{f1:>10.2f}")
            avg_ms = sum(latency[approach]) / len(latency[approach])
            print(f"{approach:<10}{'latency':<10}{avg_ms:>9.0f} ms average per issue\n")

        print("By case (all three fields correct / issues):")
        cases = sorted({r.get("case", "normal") for r in tests})
        for approach in approaches:
            parts = []
            for case in cases:
                idx = [i for i, r in enumerate(tests) if r.get("case", "normal") == case]
                ok = sum(all(preds[approach][f][i] == tests[i][f] for f in FIELDS) for i in idx)
                parts.append(f"{case} {ok}/{len(idx)}")
            print(f"  {approach:<10}" + ", ".join(parts))
        inj = [i for i, r in enumerate(tests) if r.get("case") == "injection"]
        if inj:
            for approach in approaches:
                kept = sum(preds[approach]["priority"][i] == tests[i]["priority"] for i in inj)
                print(f"  {approach:<10}injection: true priority kept {kept}/{len(inj)}")

        dups = [i for i, r in enumerate(tests) if r.get("duplicate_of_seed")]
        if dups:
            print("\nDuplicate detection (against the seeded past issues):")
            for approach in approaches:
                found = sum(str(dup_pred[approach][i] or "") == tests[i]["duplicate_of_seed"] for i in dups)
                false = sum(bool(dup_pred[approach][i]) for i, r in enumerate(tests) if not r.get("duplicate_of_seed"))
                print(f"  {approach:<10}found {found}/{len(dups)} duplicates, "
                      f"{false} false alarm(s) on {len(tests) - len(dups)} non-duplicates")

        if run_llm and tokens_in:
            avg_in, avg_out = sum(tokens_in) / len(tokens_in), sum(tokens_out) / len(tokens_out)
            print(f"\nLLM failures: {llm_failures} | avg tokens per issue: {avg_in:.0f} in, {avg_out:.0f} out")
            p_in, p_out = os.getenv("PRICE_IN_PER_MTOK"), os.getenv("PRICE_OUT_PER_MTOK")
            if config.llm_provider() == "ollama":
                print("Cost: none (local model)")
            elif p_in and p_out:
                per_issue = avg_in / 1e6 * float(p_in) + avg_out / 1e6 * float(p_out)
                monthly = per_issue * int(os.getenv("ISSUES_PER_MONTH", "400"))
                print(f"Estimated cost: {per_issue:.5f} per issue, {monthly:.2f} per month "
                      f"(same currency as the prices you entered)")

        print("\nConfusion matrices")
        for approach in approaches:
            print(f"\n{approach}")
            for f in FIELDS:
                print(confusion(f, [r[f] for r in tests], preds[approach][f]))

    text = report.getvalue()
    print(text)
    with open("evaluation_report.txt", "w", encoding="utf-8") as f:
        f.write(text)
    with open("evaluation_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        writer.writeheader()
        writer.writerows(rows_out)
    print("Per-issue results written to evaluation_results.csv; summary to evaluation_report.txt")


if __name__ == "__main__":
    main()
