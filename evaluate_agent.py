"""Score the triage agent offline on the labelled test set (data/eval_issues.csv).

Usage (needs a model, e.g. LLM_PROVIDER=ollama in .env):
    python evaluate_agent.py              # all 50 issues
    python evaluate_agent.py --limit 10   # quick check

For each issue the agent investigates with its tools and submits a plan, exactly as in the app,
against a throwaway copy of the seeded database. The plan is then checked:

- action:      'request_info' expected for vague issues, 'assign' for the rest
- module and priority of the plan's classification against the true labels
- assignee:    the developer who owns the true module (the rule-based top choice)
- corrections: how often the app had to fix the plan (assignee outside the shortlist,
               invented issue ids, invalid labels), from the plan's notes
- finished:    whether the agent submitted a plan within the step limit
- steps, tokens and time per issue

Writes agent_evaluation.csv (one row per issue) and agent_evaluation.txt (the summary).
"""
import argparse
import csv
import os
import tempfile
import time

from triage import config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    config.load_env_file()
    if not config.llm_enabled():
        raise SystemExit("No model configured. Set LLM_PROVIDER=ollama (free) or ANTHROPIC_API_KEY in .env.")

    tmp = tempfile.TemporaryDirectory()
    os.environ["DB_PATH"] = os.path.join(tmp.name, "agent_eval.db")  # never touch the real data
    from triage import db
    from triage.agent import AgentError
    from triage.llm import LLMError
    from triage.service import TriageService

    db.init_db()
    service = TriageService()
    owner = {}
    for _username, _name, role, modules in db.DEMO_USERS:
        if role == "developer":
            user = db.get_user_by_username(_username)
            for m in modules:
                owner.setdefault(m, user["id"])

    with open(config.EVAL_CSV, newline="", encoding="utf-8") as f:
        tests = list(csv.DictReader(f))[:args.limit]

    rows = []
    for n, t in enumerate(tests, 1):
        print(f"  [{n}/{len(tests)}] {t['title'][:60]}", flush=True)
        ai = service._analyse(t["title"], t["description"])
        issue_id = db.insert_issue(t["title"], t["description"], None, ai)
        issue = db.get_issue(issue_id)
        expected_action = "request_info" if t.get("case") == "vague" else "assign"
        started = time.perf_counter()
        try:
            plan, _candidates, _trace, usage = service.agent.plan(issue)
            finished, error = True, ""
        except (AgentError, LLMError) as exc:
            plan, usage, finished, error = None, {}, False, str(exc)
        row = {"title": t["title"], "case": t.get("case", "normal"), "finished": finished,
               "error": error, "expected_action": expected_action,
               "classifier_source": ai["ai_source"],
               "seconds": round(time.perf_counter() - started, 1)}
        if plan:
            cls = plan["classification"]
            row |= {"action": plan["action"], "action_ok": plan["action"] == expected_action,
                    "module_ok": cls["module"] == t["module"],
                    "priority_ok": cls["priority"] == t["priority"],
                    "assignee_ok": plan["assignee_id"] == owner.get(t["module"]),
                    "corrections": len(plan["notes"]), "notes": " | ".join(plan["notes"]),
                    "steps": usage.get("steps"), "tokens_in": usage.get("input_tokens"),
                    "tokens_out": usage.get("output_tokens")}
        rows.append(row)

    done = [r for r in rows if r["finished"]]

    def rate(key):
        return f"{sum(bool(r.get(key)) for r in done)}/{len(done)}" if done else "-"

    def avg(key):
        vals = [r[key] for r in done if r.get(key) is not None]
        return f"{sum(vals) / len(vals):.1f}" if vals else "-"

    lines = [
        f"Agent evaluation: {len(rows)} issues | model {config.llm_model()} via {config.llm_provider()}",
        f"Finished with a plan:        {len(done)}/{len(rows)} (the rest fall back to the rules plan in the app)",
        f"Right next step:             {rate('action_ok')}",
        f"Module correct:              {rate('module_ok')}",
        f"Priority correct:            {rate('priority_ok')}",
        f"Assignee = module owner:     {rate('assignee_ok')}",
        f"Plans needing a correction:  {sum(1 for r in done if r.get('corrections'))}/{len(done)}",
        f"Average steps:               {avg('steps')}",
        f"Average tokens in / out:     {avg('tokens_in')} / {avg('tokens_out')}",
        f"Average seconds per issue:   {avg('seconds')}",
    ]
    vague = [r for r in done if r["case"] == "vague"]
    if vague:
        lines.append(f"Vague issues sent back to the reporter: "
                     f"{sum(r['action'] == 'request_info' for r in vague)}/{len(vague)}")
    text = "\n".join(lines)
    print("\n" + text)
    with open("agent_evaluation.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open("agent_evaluation.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    print("Per-issue results written to agent_evaluation.csv; summary to agent_evaluation.txt")
    tmp.cleanup()


if __name__ == "__main__":
    main()
