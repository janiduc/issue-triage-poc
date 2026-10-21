"""GitHub Issues connector (REST API, fine-grained personal access token).

Token permissions needed on the repository: Issues (read and write), Metadata (read).
"""
from urllib.parse import quote

from .base import ExternalIssue, Reply, SyncError, TrackerConnector, is_ours, stale_labels, with_marker
from .http import call, valid_signature

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # up to 1,000 items per call; enough for a proof of concept


class GitHubConnector(TrackerConnector):
    name = "github"
    label = "GitHub Issues"

    def __init__(self, token, repo, webhook_secret=None, api=API):
        self.token, self.repo, self.secret, self.api = token, repo, webhook_secret, api

    def configured(self):
        return bool(self.token and self.repo and "/" in self.repo)

    def _req(self, method, path, **kw):
        return call(method, f"{self.api}/repos/{self.repo}{path}", service="GitHub",
                    headers={"Authorization": f"Bearer {self.token}",
                             "Accept": "application/vnd.github+json",
                             "X-GitHub-Api-Version": "2022-11-28"}, **kw)

    def _ext(self, item):
        return ExternalIssue(external_id=str(item["number"]), key=f"#{item['number']}",
                             url=item["html_url"], title=item["title"], body=item.get("body") or "",
                             reporter=(item.get("user") or {}).get("login", "unknown"))

    def _paged(self, path, params=None):
        """GET every page of a list endpoint (GitHub returns at most PER_PAGE per page)."""
        items = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._req("GET", path, params={**(params or {}), "per_page": PER_PAGE,
                                                   "page": page}) or []
            items += batch
            if len(batch) < PER_PAGE:
                break
        return items

    def _issues(self, state, since):
        params = {"state": state, "sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        # The issues endpoint also returns pull requests; skip them.
        return [i for i in self._paged("/issues", params) if "pull_request" not in i]

    def fetch_open_issues(self, since=None):
        return [self._ext(i) for i in self._issues("open", since)
                if "triage-desk" not in [l["name"] for l in i.get("labels", [])]]

    def fetch_replies(self, external_id, since):
        comments = self._paged(f"/issues/{external_id}/comments",
                               {"since": since} if since else {})
        return [Reply(c["user"]["login"], c["body"], c["created_at"]) for c in comments
                if not is_ours(c["body"]) and c["created_at"] >= (since or "")]

    def fetch_closed_issues(self, since=None):
        out = []
        for item in self._issues("closed", since):
            if item.get("state_reason") == "not_planned":
                continue
            ext = self._ext(item)
            comments = self._paged(f"/issues/{item['number']}/comments")
            human = [c["body"] for c in comments if not is_ours(c["body"])]
            ext.resolution = human[-1] if human else None
            out.append(ext)
        return out

    def apply_triage(self, external_id, write):
        # Replace labels from an earlier triage (e.g. an old priority) but keep people's own labels.
        current = [l["name"] for l in self._req("GET", f"/issues/{external_id}/labels") or []]
        for name in stale_labels(current, write.labels):
            self._req("DELETE", f"/issues/{external_id}/labels/{quote(name, safe='')}")
        self._req("POST", f"/issues/{external_id}/labels", json={"labels": write.labels})
        if write.assignee:
            self._req("POST", f"/issues/{external_id}/assignees", json={"assignees": [write.assignee]})
        if write.comment:
            self.post_comment(external_id, write.comment)

    def post_comment(self, external_id, text):
        self._req("POST", f"/issues/{external_id}/comments", json={"body": with_marker(text)})

    def close_issue(self, external_id, comment):
        self.post_comment(external_id, comment)
        self._req("PATCH", f"/issues/{external_id}", json={"state": "closed", "state_reason": "completed"})

    def check(self):
        if not self.configured():
            return {"ok": False, "assignable": [],
                    "message": "Token or repository missing: check the token's environment variable."}
        try:
            self._req("GET", "")
            assignable = [a["login"] for a in self._paged("/assignees")]
        except SyncError as exc:
            return {"ok": False, "message": str(exc), "assignable": []}
        return {"ok": True, "message": f"Connected to {self.repo}.", "assignable": assignable}

    def account_for(self, user_row):
        return user_row["github_login"] if user_row else None

    def verify_webhook(self, headers, raw_body, query):
        return valid_signature(self.secret, raw_body, headers.get("X-Hub-Signature-256"))

    def parse_webhook(self, headers, payload):
        event, action = headers.get("X-GitHub-Event"), payload.get("action")
        issue = payload.get("issue") or {}
        if "pull_request" in issue:
            return None
        if event == "issues" and action == "opened":
            return "issue", self._ext(issue)
        if event == "issue_comment" and action == "created" and not is_ours(payload["comment"]["body"]):
            c = payload["comment"]
            return "reply", (str(issue["number"]), Reply(c["user"]["login"], c["body"], c["created_at"]))
        if event == "issues" and action == "closed" and issue.get("state_reason") != "not_planned":
            return "closed", self._ext(issue)
        return None
