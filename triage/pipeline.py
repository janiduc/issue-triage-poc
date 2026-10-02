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
from .integrations.base import SyncError, TriageWrite

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, service, connector):
        self.service, self.connector = service, connector

    @property
    def name(self):
        return self.connector.name

    def enabled(self):
        return self.connector.configured()

    # ---------------- inbound ----------------

    def ingest(self, ext):
        existing = db.find_external(self.name, ext.external_id)
        if existing:
            return existing, False
        issue_id = self.service.triage_new(
            (ext.title or "(no title)")[:200], (ext.body or "(no description)")[:5000], None,
            external={"source": self.name, "external_id": ext.external_id, "external_key": ext.key,
                      "external_url": ext.url, "external_reporter": ext.reporter})
        db.log_sync(issue_id, self.name, "in", "issue_imported", True, ext.key)
        return issue_id, True

    def handle_replies(self, external_id, replies):
        issue_id = db.find_external(self.name, external_id)
        issue = db.get_issue(issue_id) if issue_id else None
        if not issue or issue["status"] != "needs_info" or not replies:
            return False
        text = "\n".join(f"{r.body} (from {r.author})" for r in replies)[:3000]
        self.service.retriage_with_info(issue, text)
        db.log_sync(issue_id, self.name, "in", "reply_imported", True, f"{len(replies)} reply(ies)")
        return True

    def handle_closed(self, ext):
        issue_id = db.find_external(self.name, ext.external_id)
        issue = db.get_issue(issue_id) if issue_id else None
        if not issue or issue["status"] == "resolved":
            return False
        db.save_resolution(issue_id, ext.resolution, None)  # None: no note, so not reused as a fix
        db.set_sync(issue_id, sync_pending=None, sync_status="ok", sync_error=None)
        self.service.refresh()
        db.log_sync(issue_id, self.name, "in", "closed_in_tracker", True,
                    "with resolution note" if ext.resolution else "no resolution note")
        return True

    def sync(self):
        if not self.enabled():
            raise SyncError("No issue tracker is configured")
        since_key = f"sync_since_{self.name}"
        since, started = db.get_setting(since_key), db.now()
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
            for issue in db.linked_issues(self.name, "needs_info"):
                found = self.connector.fetch_replies(issue["external_id"], issue["info_requested_at"])
                summary["replies"] += self.handle_replies(issue["external_id"], found)

        def closed():
            for ext in self.connector.fetch_closed_issues(since):
                summary["closed"] += self.handle_closed(ext)

        attempt("import", imports)
        attempt("replies", replies)
        attempt("closed issues", closed)
        for issue_id in db.failed_syncs():
            summary["retried"] += self.deliver(issue_id)
        if not summary["errors"]:
            db.set_setting(since_key, started)  # only advance when everything succeeded
        db.set_setting(f"last_sync_{self.name}", started)
        db.log_sync(None, self.name, "in", "sync_run", not summary["errors"], json.dumps(summary))
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
        if not issue or issue["source"] != self.name or not self.enabled():
            return None
        outbox = json.loads(issue["sync_pending"] or "[]")
        outbox.append({"action": action, **payload})
        db.set_sync(issue_id, sync_pending=json.dumps(outbox), sync_status="pending")
        return self.deliver(issue_id)

    def triage_write(self, issue, plan_labels_extra, comment):
        dev = db.get_user(issue["assignee_id"]) if issue["assignee_id"] else None
        account = self.connector.account_for(dev)
        cls = {"type": issue["final_type"], "module": issue["final_module"],
               "priority": issue["final_priority"]}
        return {"labels": base_labels(cls) + list(plan_labels_extra or []),
                "priority": cls["priority"], "assignee": account, "comment": comment,
                "warning": None if account or not dev else
                f"{dev['display_name']} has no {self.connector.label} account mapped; not assigned there"}

    def deliver(self, issue_id):
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
