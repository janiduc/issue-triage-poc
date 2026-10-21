"""Triage workflow: redact -> find similar -> LLM (or fallback) -> review flags -> store.
Also suggests which developer should own an issue."""
import json
import time

import logging

from . import config, db
from .agent import AgentError, TriageAgent, rules_plan
from .baseline import BaselineClassifier
from .llm import LLMError, triage_with_llm
from .redact import redact
from .similarity import SimilarityIndex

STATUS_LABELS = {"new": "Awaiting triage", "needs_info": "More information needed",
                 "assigned": "Assigned to a developer", "resolved": "Resolved"}


def ai_enabled() -> bool:
    """API key configured AND not switched off by an administrator."""
    return config.llm_enabled() and db.get_setting("llm_enabled", "true") == "true"


def agent_enabled() -> bool:
    return ai_enabled() and db.get_setting("agent_enabled", "true") == "true"


log = logging.getLogger(__name__)


def confidence_threshold() -> float:
    return float(db.get_setting("confidence_threshold", config.CONFIDENCE_THRESHOLD))


MIN_PROJECT_LABELS = 20  # below this, a project's backup classifier learns from the seed data


class TriageService:
    """Shared AI workflow; each project has its own knowledge base and backup classifier."""

    def __init__(self):
        self.indexes, self.baselines = {}, {}
        self.agent = TriageAgent(self)
        self.refresh()

    @property
    def index(self):  # the first project's index (health endpoint, single-project callers)
        return self.index_for(db.default_project_id())

    def refresh(self, project_id=None):
        """Rebuild the similarity index and retrain the backup classifier: for one project, or
        for every project when none is given."""
        ids = [project_id] if project_id is not None else [p["id"] for p in db.list_projects()]
        for pid in ids:
            project = db.get_project(pid)
            self.indexes[pid] = SimilarityIndex().build(
                db.knowledge_base(pid, include_seed=bool(project and project["share_seed"])))
            labelled = db.labelled_issues(pid)
            if len(labelled) < MIN_PROJECT_LABELS:
                labelled = db.labelled_issues(seed_only=True) + labelled
            self.baselines[pid] = BaselineClassifier().fit(labelled)

    def index_for(self, project_id):
        if project_id not in self.indexes:
            self.refresh(project_id)
        return self.indexes[project_id]

    def baseline_for(self, project_id):
        if project_id not in self.baselines:
            self.refresh(project_id)
        return self.baselines[project_id]

    @staticmethod
    def modules_for(project_id):
        project = db.get_project(project_id) if project_id is not None else None
        return (project or {}).get("modules") or config.MODULES

    # ----- public workflow -----

    def triage_new(self, title, description, reporter_id, external=None, project_id=None):
        title, n1 = redact(title)
        description, n2 = redact(description)
        if external is None and project_id is not None:
            external = {"source": "desk", "project_id": project_id}
        if project_id is None:
            project_id = (external or {}).get("project_id") or db.default_project_id()
        ai = self._analyse(title, description, project_id)
        ai["redactions"] = n1 + n2
        issue_id = db.insert_issue(title, description, reporter_id, ai, external)
        self.create_plan(issue_id)
        return issue_id

    def retriage_with_info(self, issue, extra):
        """Reporter supplied more detail: append it, re-run the analysis and the agent."""
        extra, n = redact(extra)
        description = f"{issue['description']}\n\nAdditional information: {extra}"
        ai = self._analyse(issue["title"], description, issue["project_id"])
        ai["redactions"] = (issue.get("redactions") or 0) + n
        db.update_after_retriage(issue["id"], description, ai)
        self.create_plan(issue["id"])

    def create_plan(self, issue_id):
        """Ask the agent for an action plan; fall back to the rules planner if it can't."""
        issue = db.get_issue(issue_id)
        reason = None
        if agent_enabled():
            try:
                plan, candidates, trace, usage = self.agent.plan(issue)
                return db.insert_plan(issue_id, "agent", plan, candidates, trace, usage=usage)
            except (AgentError, LLMError) as exc:
                reason = str(exc)
            except Exception as exc:  # malformed response etc.: never block triage
                log.exception("Agent failed")
                reason = f"Agent error ({type(exc).__name__})"
        else:
            reason = "Agent switched off or AI service not configured"
        plan, candidates = rules_plan(issue, self.index_for(issue["project_id"]), confidence_threshold())
        return db.insert_plan(issue_id, "rules", plan, candidates, [], fallback_reason=reason)

    # ----- internals -----

    def _analyse(self, title, description, project_id=None):
        started = time.perf_counter()
        if project_id is None:
            project_id = db.default_project_id()
        modules = self.modules_for(project_id)
        index = self.index_for(project_id)
        text = f"{title}. {description}"
        similar = index.search(text)

        source, fallback_reason, usage = "llm", None, {}
        if not ai_enabled():
            fallback_reason = "AI service not configured or switched off by an administrator"
        else:
            try:
                result, usage = triage_with_llm(title, description, similar, modules)
            except LLMError as exc:
                fallback_reason = str(exc)
        if fallback_reason:
            source = "fallback"
            result = self._fallback(text, similar, project_id, modules, index)

        reasons = []
        if source == "fallback":
            reasons.append("Produced by the backup classifier, not the AI model")
        if len(description.split()) < config.MIN_DESCRIPTION_WORDS:
            reasons.append("Description is too short; consider asking the reporter for steps to reproduce")
        if result["confidence"] < confidence_threshold():
            reasons.append(f"Low confidence ({result['confidence']:.2f})")
        if result["priority"] == "critical":
            reasons.append("Critical priority: confirm and escalate now")

        return {
            "ai_type": result["type"], "ai_module": result["module"],
            "ai_priority": result["priority"], "ai_confidence": result["confidence"],
            "ai_justification": result["justification"], "ai_source": source,
            "fallback_reason": fallback_reason, "needs_review": int(bool(reasons)),
            "review_reasons": json.dumps(reasons), "duplicate_of": result["duplicate_of"],
            "recommendation": result["recommendation"],
            "recommendation_sources": json.dumps(result["recommendation_sources"]),
            "similar_json": json.dumps(similar),
            "latency_ms": usage.get("latency_ms") or int((time.perf_counter() - started) * 1000),
            "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens"),
        }

    def _fallback(self, text, similar, project_id, modules, index):
        pred = self.baseline_for(project_id).predict(text)
        if pred["module"] not in modules:  # learned from seed data with other modules
            pred["module"] = "other" if "other" in modules else modules[0]
        top = similar[0] if similar else None
        is_dup = bool(top and top["similarity"] >= index.duplicate_threshold)
        return {
            "type": pred["type"], "module": pred["module"], "priority": pred["priority"],
            "confidence": pred["confidence"],
            "justification": "Predicted by the backup keyword-statistics classifier. "
                             "No written explanation is available in backup mode.",
            "duplicate_of": top["id"] if is_dup else None,
            # No text generation in fallback: reuse the past fix verbatim, clearly attributed.
            "recommendation": top["resolution"] if is_dup else None,
            "recommendation_sources": [top["id"]] if is_dup else [],
        }


def to_api(row, role):
    """Shape an issue for the requesting role.

    Reporters (who may be external clients) get only their own issue's status and outcome:
    no AI internals and no similar issues, because those contain other clients' reports.
    """
    base = {
        "id": row["id"], "title": row["title"], "description": row["description"],
        "status": row["status"], "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "info_request": row.get("info_request"), "resolution": row.get("resolution"),
        "reporter_update": row.get("reporter_update"),
        "created_at": row.get("created_at"),
        "priority": row.get("final_priority") if row["status"] in ("assigned", "resolved") else None,
    }
    if role == "reporter":
        return base

    def load(value, default):
        return json.loads(value) if value else default

    return base | {
        "reporter_name": row.get("reporter_name"),
        "source": row.get("source"), "external_key": row.get("external_key"),
        "external_url": row.get("external_url"), "sync_status": row.get("sync_status"),
        "sync_error": row.get("sync_error"),
        "assignee_id": row.get("assignee_id"), "assignee_name": row.get("assignee_name"),
        "suggestion": {
            "type": row["ai_type"], "module": row["ai_module"], "priority": row["ai_priority"],
            "confidence": row["ai_confidence"], "justification": row["ai_justification"],
            "source": row["ai_source"], "fallback_reason": row.get("fallback_reason"),
        },
        "needs_review": bool(row.get("needs_review")),
        "review_reasons": load(row.get("review_reasons"), []),
        "duplicate_of": row.get("duplicate_of"),
        "recommendation": row.get("recommendation"),
        "recommendation_sources": load(row.get("recommendation_sources"), []),
        "similar_issues": load(row.get("similar_json"), []),
        "redactions": row.get("redactions") or 0,
        "triage_count": row.get("triage_count") or 0,
        "final": {"type": row.get("final_type"), "module": row.get("final_module"),
                  "priority": row.get("final_priority"), "action": row.get("review_action"),
                  "note": row.get("review_note")},
    }
