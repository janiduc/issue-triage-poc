"""Jira Cloud connector (REST API v3, email + API token).

Uses /rest/api/3/search/jql (the older /search endpoint was removed from Jira Cloud).
Descriptions and comments are Atlassian Document Format (ADF) and are converted to text.
"""
from datetime import datetime, timezone

from .base import ExternalIssue, Reply, SyncError, TrackerConnector, is_ours, with_marker
from .http import call, valid_signature

# Desk priority -> Jira's default priority scheme. Adjust if the project uses custom names.
PRIORITY_MAP = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}
MAX_PAGES = 5


def adf_to_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return "\n"
    inner = "".join(adf_to_text(c) for c in node.get("content", []))
    return inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem") else "")


def to_utc(ts):
    """Jira returns e.g. 2026-09-29T15:30:00.000+0530; convert to comparable UTC ISO text."""
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ts or ""


def text_to_adf(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []}
        for line in text.split("\n")]}


class JiraConnector(TrackerConnector):
    name = "jira"
    label = "Jira"

    def __init__(self, base_url, email, token, project, webhook_secret=None):
        self.base = (base_url or "").rstrip("/")
        self.email, self.token, self.project, self.secret = email, token, project, webhook_secret

    def configured(self):
        return bool(self.base and self.email and self.token and self.project)

    def _req(self, method, path, **kw):
        return call(method, f"{self.base}/rest/api/3{path}", service="Jira",
                    auth=(self.email, self.token),
                    headers={"Accept": "application/json", "Content-Type": "application/json"}, **kw)

    def _ext(self, item):
        f = item["fields"]
        return ExternalIssue(external_id=item["key"], key=item["key"],
                             url=f"{self.base}/browse/{item['key']}", title=f.get("summary") or "",
                             body=adf_to_text(f.get("description")).strip(),
                             reporter=(f.get("reporter") or {}).get("displayName", "unknown"))

    def _search(self, jql, fields):
        issues, token = [], None
        for _ in range(MAX_PAGES):
            params = {"jql": jql, "fields": fields, "maxResults": 50}
            if token:
                params["nextPageToken"] = token
            page = self._req("GET", "/search/jql", params=params) or {}
            issues += page.get("issues", [])
            token = page.get("nextPageToken")
            if not token:
                break
        return issues

    @staticmethod
    def _jql_time(since):
        return since[:16].replace("T", " ")  # Jira JQL accepts "yyyy-MM-dd HH:mm"

    def fetch_open_issues(self, since=None):
        jql = (f'project = "{self.project}" AND statusCategory = "To Do" '
               f'AND (labels IS EMPTY OR labels != "triage-desk")')
        if since:
            jql += f' AND created >= "{self._jql_time(since)}"'
        return [self._ext(i) for i in self._search(jql + " ORDER BY created ASC",
                                                   "summary,description,reporter")]

    def fetch_replies(self, external_id, since):
        data = self._req("GET", f"/issue/{external_id}/comment", params={"orderBy": "created"}) or {}
        out = []
        for c in data.get("comments", []):
            body = adf_to_text(c.get("body")).strip()
            created = to_utc(c.get("created"))
            if created >= (since or "") and not is_ours(body):
                out.append(Reply(c.get("author", {}).get("displayName", "unknown"), body, created))
        return out

    def fetch_closed_issues(self, since=None):
        jql = f'project = "{self.project}" AND statusCategory = Done'
        if since:
            jql += f' AND resolutiondate >= "{self._jql_time(since)}"'
        out = []
        for item in self._search(jql, "summary,description,reporter,comment"):
            ext = self._ext(item)
            comments = ((item["fields"].get("comment") or {}).get("comments")) or []
            human = [adf_to_text(c.get("body")).strip() for c in comments]
            human = [h for h in human if h and not is_ours(h)]
            ext.resolution = human[-1] if human else None
            out.append(ext)
        return out

    def apply_triage(self, external_id, write):
        self._req("PUT", f"/issue/{external_id}", json={
            "update": {"labels": [{"add": l} for l in write.labels]},
            "fields": {"priority": {"name": PRIORITY_MAP[write.priority]}}})
        if write.assignee:
            self._req("PUT", f"/issue/{external_id}/assignee", json={"accountId": write.assignee})
        if write.comment:
            self.post_comment(external_id, write.comment)

    def post_comment(self, external_id, text):
        self._req("POST", f"/issue/{external_id}/comment", json={"body": text_to_adf(with_marker(text))})

    def close_issue(self, external_id, comment):
        self.post_comment(external_id, comment)
        transitions = (self._req("GET", f"/issue/{external_id}/transitions") or {}).get("transitions", [])
        done = [t for t in transitions
                if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done"]
        if not done:
            raise SyncError("Jira: no transition to a Done status is available for this issue")
        self._req("POST", f"/issue/{external_id}/transitions", json={"transition": {"id": done[0]["id"]}})

    def account_for(self, user_row):
        return user_row["jira_account_id"] if user_row else None

    def verify_webhook(self, headers, raw_body, query):
        return valid_signature(self.secret, raw_body, headers.get("X-Hub-Signature"))

    def parse_webhook(self, headers, payload):
        event = payload.get("webhookEvent")
        issue = payload.get("issue")
        if event == "jira:issue_created" and issue:
            return "issue", self._ext(issue)
        if event == "comment_created" and issue and payload.get("comment"):
            c = payload["comment"]
            body = adf_to_text(c.get("body")).strip() if isinstance(c.get("body"), dict) else c.get("body", "")
            if not is_ours(body):
                return "reply", (issue["key"], Reply(c.get("author", {}).get("displayName", "unknown"),
                                                     body, c.get("created", "")))
        if event == "jira:issue_updated" and issue:
            category = (((issue["fields"].get("status") or {}).get("statusCategory")) or {}).get("key")
            if category == "done":
                return "closed", self._ext(issue)
        return None
