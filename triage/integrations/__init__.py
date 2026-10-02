"""Choose the issue-tracker connector from environment variables (secrets stay out of the database)."""
import os

from .. import config
from .base import TrackerConnector
from .github import GitHubConnector
from .jira import JiraConnector
from .simulated import SimulatedConnector


def get_connector():
    tracker = os.getenv("TRACKER", "simulated").lower()
    if tracker == "github":
        return GitHubConnector(os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO"),
                               os.getenv("GITHUB_WEBHOOK_SECRET"))
    if tracker == "jira":
        return JiraConnector(os.getenv("JIRA_BASE_URL"), os.getenv("JIRA_EMAIL"),
                             os.getenv("JIRA_API_TOKEN"), os.getenv("JIRA_PROJECT"),
                             os.getenv("JIRA_WEBHOOK_SECRET"))
    if tracker == "simulated":
        return SimulatedConnector(os.getenv("SIM_TRACKER_PATH",
                                            str(config.BASE_DIR / "simulated_tracker.json")))
    return TrackerConnector()
