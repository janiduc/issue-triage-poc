"""A simulated tracker stored in a JSON file, for offline demos and tests.

It behaves like a tiny GitHub/Jira: issues with labels, assignee and comments. Everything the
desk writes back is visible in the Integrations screen, so write-back can be demonstrated
without a real account. Clearly a simulation, and disclosed as such in the README.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .base import ExternalIssue, Reply, SyncError, TrackerConnector, is_ours, with_marker

SEED = config.BASE_DIR / "data" / "simulated_tracker_seed.json"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SimulatedConnector(TrackerConnector):
    name = "simulated"
    label = "Simulated tracker"

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            shutil.copy(SEED, self.path)

    def configured(self):
        return True

    def _load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state):
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _get(self, state, key):
        for issue in state["issues"]:
            if issue["key"] == key:
                return issue
        raise SyncError(f"{key} not found in the simulated tracker")

    def _ext(self, i):
        return ExternalIssue(external_id=i["key"], key=i["key"], url=f"simulated://{i['key']}",
                             title=i["title"], body=i["body"], reporter=i["reporter"])

    # --- reads ---
    def fetch_open_issues(self, since=None):
        return [self._ext(i) for i in self._load()["issues"]
                if i["state"] == "open" and (not since or i["created_at"] >= since)]

    def fetch_replies(self, external_id, since):
        issue = self._get(self._load(), external_id)
        return [Reply(c["author"], c["body"], c["created_at"]) for c in issue["comments"]
                if c["created_at"] >= (since or "") and not is_ours(c["body"])]

    def fetch_closed_issues(self, since=None):
        out = []
        for i in self._load()["issues"]:
            if i["state"] == "closed" and (not since or (i.get("closed_at") or "") >= since):
                human = [c["body"] for c in i["comments"] if not is_ours(c["body"])]
                ext = self._ext(i)
                ext.resolution = human[-1] if human else None
                out.append(ext)
        return out

    # --- writes ---
    def apply_triage(self, external_id, write):
        state = self._load()
        issue = self._get(state, external_id)
        issue["labels"] = sorted(set(issue["labels"]) | set(write.labels))
        issue["priority"] = write.priority
        if write.assignee:
            issue["assignee"] = write.assignee
        if write.comment:
            issue["comments"].append({"author": "triage-desk", "body": with_marker(write.comment),
                                      "created_at": _now()})
        self._save(state)

    def post_comment(self, external_id, text):
        state = self._load()
        self._get(state, external_id)["comments"].append(
            {"author": "triage-desk", "body": with_marker(text), "created_at": _now()})
        self._save(state)

    def close_issue(self, external_id, comment):
        state = self._load()
        issue = self._get(state, external_id)
        issue["comments"].append({"author": "triage-desk", "body": with_marker(comment),
                                  "created_at": _now()})
        issue["state"], issue["closed_at"] = "closed", _now()
        self._save(state)

    def account_for(self, user_row):
        return user_row["username"] if user_row else None

    # --- demo helpers (act as the reporter or as someone working directly in the tracker) ---
    def state(self):
        return self._load()

    def create_issue(self, title, body, reporter):
        state = self._load()
        n = max((int(i["key"].split("-")[1]) for i in state["issues"]), default=0) + 1
        issue = {"key": f"SIM-{n}", "title": title, "body": body, "reporter": reporter,
                 "state": "open", "labels": [], "priority": None, "assignee": None,
                 "comments": [], "created_at": _now()}
        state["issues"].append(issue)
        self._save(state)
        return issue

    def add_reply(self, key, author, body):
        state = self._load()
        self._get(state, key)["comments"].append({"author": author, "body": body, "created_at": _now()})
        self._save(state)

    def close_externally(self, key, author, body):
        state = self._load()
        issue = self._get(state, key)
        issue["comments"].append({"author": author, "body": body, "created_at": _now()})
        issue["state"], issue["closed_at"] = "closed", _now()
        self._save(state)
