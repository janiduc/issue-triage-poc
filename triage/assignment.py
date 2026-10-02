"""Rule-based developer ranking: transparent, explainable, and always approved by a human.

The AI agent may only choose an assignee from this shortlist, and the triage lead approves
or changes the choice. The weights are deliberately simple so the lead can check them.
"""
from . import db

WEIGHTS = {
    "owns_module": 3.0,          # developer is the named owner of the issue's module
    "fixed_similar": 2.0,        # per similar past issue this developer resolved (max 2 counted)
    "module_experience": 0.5,    # per past fix in this module (max 4 counted)
    "open_issue": -1.0,          # per issue currently assigned and still open
}


def rank_developers(module, similar_issue_ids=(), limit=3):
    devs = db.developer_workload()
    similar_fixes = db.resolvers_for(list(similar_issue_ids))
    module_fixes = db.module_fix_counts(module)
    ranked = []
    for d in devs:
        owns = module in d["modules"]
        fixed = similar_fixes.get(d["id"], [])
        experience = module_fixes.get(d["id"], 0)
        parts = [
            ("owns_module", 1 if owns else 0,
             f"owns the {module.replace('_', ' ')} module" if owns else None),
            ("fixed_similar", min(len(fixed), 2),
             f"fixed similar issue {', '.join('#' + str(i) for i in fixed)}" if fixed else None),
            ("module_experience", min(experience, 4),
             f"{experience} past fix(es) in this module" if experience else None),
            ("open_issue", d["open_count"], f"{d['open_count']} open assignment(s)"),
        ]
        score = sum(WEIGHTS[k] * n for k, n, _ in parts)
        ranked.append({
            "id": d["id"], "name": d["display_name"], "score": round(score, 1),
            "open_count": d["open_count"], "modules": d["modules"],
            "reasons": [text for _, _, text in parts if text],
        })
    ranked.sort(key=lambda c: (-c["score"], c["open_count"], c["id"]))
    return ranked[:limit]
