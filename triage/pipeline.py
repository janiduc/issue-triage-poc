"""End-to-end pipeline between the issue tracker and the triage desk.

Inbound  (tracker -> desk): new issues are imported and triaged; reporter replies to a
         question trigger re-analysis; issues closed in the tracker feed the knowledge base.
         Runs from webhooks (instant) or polling ("Sync now" / background interval).
Outbound (desk -> tracker): only after a human approves. Labels, priority, assignee and a
         comment on triage; a question when more information is needed; a closing comment
         on resolution.

Outbound writes go through an outbox (issues.sync_pending). If the tracker is down the write
stays queued, the issue shows "sync failed", and it is retried on the next sync or on demand.
Inbound imports are idempotent: the (source, external_id) pair is unique.
"""
import json
import logging

from . import db
from .agent import base_labels
from .integrations import connector_for, connector_signature
from .integrations.base import SyncError, TriageWrite

log = logging.getLogger(__name__)


class Pipelines:
    """One Pipeline per project, built from the project's row and rebuilt when it changes."""

    def __init__(self, service):
        self.service = service
        self._cache = {}

    def get(self, project_id):
        project = db.get_project(project_id)
        if not project:
            return None
        signature = connector_signature(project)
        cached = self._cache.get(project_id)
        if not cached or cached[0] != signature:
            cached = (signature, Pipeline(self.service, connector_for(project), project))
            self._cache[project_id] = cached
        cached[1].project = project
        return cached[1]

    def default(self):
        return self.get(db.default_project_id())

    def for_issue(self, issue):
        return self.get(issue["project_id"]) if issue else None

    def by_slug(self, slug):
        project = db.get_project_by_slug(slug)
        return self.get(project["id"]) if project else None

    def all(self, active_only=True):
        return [self.get(p["id"]) for p in db.list_projects(active_only)]


class Pipeline:
    def __init__(self, service, connector, project=None):
        self.service, self.connector = service, connector
        self.project = project or db.get_project(db.default_project_id())

    @property
    def project_id(self):
        return self.project["id"]

    @property
    def name(self):
        return self.connector.name

    def enabled(self):
        return self.connector.configured()

    # ---------------- inbound ----------------

    def ingest(self, ext):
        existing = db.find_external(self.name, ext.external_id, self.project_id)
        if existing:
            return existing, False
        issue_id = self.service.triage_new(
            (ext.title or "(no title)")[:200], (ext.body or "(no description)")[:5000], None,
            external={"source": self.name, "external_id": ext.external_id, "external_key": ext.key,
                      "external_url": ext.url, "external_reporter": ext.reporter,
                      "project_id": self.project_id})
        db.log_sync(issue_id, self.name, "in", "issue_imported", True, ext.key, self.project_id)
        return issue_id, True

    def handle_replies(self, external_id, replies):
        issue_id = db.find_external(self.name, external_id, self.project_id)
        issue = db.get_issue(issue_id) if issue_id else None
        if not issue or issue["status"] != "needs_info" or not replies:
            return False
        text = "\n".join(f"{r.body} (from {r.author})" for r in replies)[:3000]
        self.service.retriage_with_info(issue, text)
        db.log_sync(issue_id, self.name, "in", "reply_imported", True, f"{len(replies)} reply(ies)")
        return True

    def handle_closed(self, ext):
        issue_id = db.find_external(self.name, ext.external_id, self.project_id)
        issue = db.get_issue(issue_id) if issue_id else None
        if not issue or issue["status"] == "resolved":
            return False
        db.save_resolution(issue_id, ext.resolution, None)  # None: no note, so not reused as a fix
        db.set_sync(issue_id, sync_pending=None, sync_status="ok", sync_error=None)
        self.service.refresh(self.project_id)
        db.log_sync(issue_id, self.name, "in", "closed_in_tracker", True,
                    "with resolution note" if ext.resolution else "no resolution note")
        return True

    def sync(self):
        if not self.enabled():
            raise SyncError("No issue tracker is configured")
        if not db.get_project(self.project_id)["active"]:
            raise SyncError("This project is archived; imports are stopped")
        project = db.get_project(self.project_id)
        since, started = project.get("sync_since"), db.now()
        summary = {"imported": 0, "replies": 0, "closed": 0, "retried": 0, "errors": []}

        def attempt(label, fn):
            try:
                fn()
            except SyncError as exc:
                summary["errors"].append(f"{label}: {exc}")

        def imports():
            for ext in self.connector.fetch_open_issues(since):
                summary["imported"] += self.ingest(ext)[1]

        def replies():
            for issue in db.linked_issues(self.name, "needs_info", self.project_id):
                found = self.connector.fetch_replies(issue["external_id"], issue["info_requested_at"])
                summary["replies"] += self.handle_replies(issue["external_id"], found)

        def closed():
            for ext in self.connector.fetch_closed_issues(since):
                summary["closed"] += self.handle_closed(ext)

        attempt("import", imports)
        attempt("replies", replies)
        attempt("closed issues", closed)
        for issue_id in db.failed_syncs(self.project_id):
            summary["retried"] += self.deliver(issue_id)
        if not summary["errors"]:
            db.update_project(self.project_id, sync_since=started)  # only advance when all succeeded
        db.update_project(self.project_id, last_sync=started)
        db.log_sync(None, self.name, "in", "sync_run", not summary["errors"], json.dumps(summary),
                    self.project_id)
        return summary

    def handle_webhook(self, kind, data):
        if kind == "issue":
            return self.ingest(data)[1]
        if kind == "reply":
            external_id, reply = data
            return self.handle_replies(external_id, [reply])
        if kind == "closed":
            return self.handle_closed(data)
        return False

    # ---------------- outbound (after human approval) ----------------

    def queue(self, issue_id, action, **payload):
        """Add a write-back to the outbox and try to deliver it now. Returns None if not linked."""
        issue = db.get_issue(issue_id)
        if (not issue or issue["source"] != self.name or issue["project_id"] != self.project_id
                or not self.enabled() or not db.get_project(self.project_id)["active"]):
            return None
        outbox = json.loads(issue["sync_pending"] or "[]")
        outbox.append({"action": action, **payload})
        db.set_sync(issue_id, sync_pending=json.dumps(outbox), sync_status="pending")
        return self.deliver(issue_id)

    def triage_write(self, issue, plan_labels_extra, comment):
        dev = db.get_user(issue["assignee_id"]) if issue["assignee_id"] else None
        member = dev is not None and self.project_id in db.memberships(dev["id"])
        account = self.connector.account_for(dev) if member else None
        cls = {"type": issue["final_type"], "module": issue["final_module"],
               "priority": issue["final_priority"]}
        return {"labels": base_labels(cls) + list(plan_labels_extra or []),
                "priority": cls["priority"], "assignee": account, "comment": comment,
                "warning": None if account or not dev else
                (f"{dev['display_name']} is not a member of this project; not assigned there" if not member
                 else f"{dev['display_name']} has no {self.connector.label} account mapped; not assigned there")}

    def deliver(self, issue_id):
        if not db.get_project(self.project_id)["active"]:
            return False  # archived: nothing more is written to its tracker
        issue = db.get_issue(issue_id)
        outbox = json.loads(issue["sync_pending"] or "[]")
        while outbox:
            item = outbox[0]
            try:
                if item["action"] == "triage":
                    self.connector.apply_triage(issue["external_id"], TriageWrite(
                        labels=item["labels"], priority=item["priority"],
                        assignee=item.get("assignee"), comment=item.get("comment")))
                elif item["action"] == "comment":
                    self.connector.post_comment(issue["external_id"], item["text"])
                elif item["action"] == "close":
                    self.connector.close_issue(issue["external_id"], item["text"])
            except SyncError as exc:
                db.set_sync(issue_id, sync_pending=json.dumps(outbox), sync_status="failed",
                            sync_error=str(exc))
                db.log_sync(issue_id, self.name, "out", item["action"], False, str(exc))
                return False
            db.log_sync(issue_id, self.name, "out", item["action"], True, item.get("warning"))
            outbox.pop(0)
        db.set_sync(issue_id, sync_pending=None, sync_status="ok", sync_error=None)
        return True
