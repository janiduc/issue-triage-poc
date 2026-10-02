"""Common interface every issue-tracker connector implements."""
from dataclasses import dataclass, field

# Hidden marker added to everything the desk posts, so its own comments are never
# mistaken for a reporter's reply or a resolution note.
MARKER = "[triage-desk]"


class SyncError(Exception):
    """A tracker call failed. The message is safe to show to users (no secrets)."""


@dataclass
class ExternalIssue:
    external_id: str          # stable id used for de-duplication (GitHub number, Jira key)
    key: str                  # human-readable reference, e.g. "#42" or "SHOP-17"
    url: str
    title: str
    body: str
    reporter: str
    resolution: str = None    # for closed issues: the closing comment, if any


@dataclass
class Reply:
    author: str
    body: str
    created_at: str


@dataclass
class TriageWrite:
    """Everything the lead approved, to be written back to the tracker."""
    labels: list
    priority: str
    assignee: str = None      # tracker account (GitHub login or Jira accountId)
    comment: str = None
    warnings: list = field(default_factory=list)


class TrackerConnector:
    name = "none"
    label = "No tracker"

    def configured(self) -> bool:
        return False

    def fetch_open_issues(self, since=None):
        return []

    def fetch_replies(self, external_id, since):
        return []

    def fetch_closed_issues(self, since=None):
        return []

    def apply_triage(self, external_id, write: TriageWrite):
        raise SyncError("No tracker configured")

    def post_comment(self, external_id, text):
        raise SyncError("No tracker configured")

    def close_issue(self, external_id, comment):
        raise SyncError("No tracker configured")

    def account_for(self, user_row):
        """The tracker account of a desk user, or None if not mapped."""
        return None

    # Webhooks
    def verify_webhook(self, headers, raw_body, query) -> bool:
        return False

    def parse_webhook(self, headers, payload):
        """Return ('issue', ExternalIssue) | ('reply', (external_id, Reply)) | ('closed', ExternalIssue) | None."""
        return None


def with_marker(text):
    return f"{text}\n\n{MARKER}"


def is_ours(text):
    return MARKER in (text or "")
