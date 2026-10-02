"""GitHub Issues connector (REST API, fine-grained personal access token).

Token permissions needed on the repository: Issues (read and write), Metadata (read).
"""
from .base import ExternalIssue, Reply, TrackerConnector, is_ours, with_marker
from .http import call, valid_signature

API = "https://api.github.com"


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

    def _issues(self, state, since):
        params = {"state": state, "per_page": 50, "sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        # The issues endpoint also returns pull requests; skip them, and skip anything
        # already carrying our label (handled previously).
        return [i for i in self._req("GET", "/issues", params=params) or []
                if "pull_request" not in i]

    def fetch_open_issues(self, since=None):
        return [self._ext(i) for i in self._issues("open", since)
                if "triage-desk" not in [l["name"] for l in i.get("labels", [])]]

    def fetch_replies(self, external_id, since):
        comments = self._req("GET", f"/issues/{external_id}/comments",
                             params={"since": since} if since else {}) or []
        return [Reply(c["user"]["login"], c["body"], c["created_at"]) for c in comments
                if not is_ours(c["body"]) and c["created_at"] >= (since or "")]

    def fetch_closed_issues(self, since=None):
        out = []
        for item in self._issues("closed", since):
            if item.get("state_reason") == "not_planned":
                continue
            ext = self._ext(item)
            comments = self._req("GET", f"/issues/{item['number']}/comments") or []
            human = [c["body"] for c in comments if not is_ours(c["body"])]
            ext.resolution = human[-1] if human else None
            out.append(ext)
        return out

    def apply_triage(self, external_id, write):
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
