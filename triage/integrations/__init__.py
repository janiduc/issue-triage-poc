"""Build each project's issue-tracker connector.

A project row stores the tracker type, the repository, and the NAMES of the environment variables
that hold its token and webhook secret; the secrets themselves stay in the environment.
"""
import os
from pathlib import Path

from .. import config
from .base import TrackerConnector
from .github import GitHubConnector
from .jira import JiraConnector
from .simulated import SimulatedConnector


def _env(name):
    return os.getenv(name) if name else None


def simulated_path(project):
    if project.get("managed_by_env"):
        return os.getenv("SIM_TRACKER_PATH", str(config.BASE_DIR / "simulated_tracker.json"))
    folder = Path(os.getenv("SIM_TRACKER_DIR", str(config.BASE_DIR)))
    return str(folder / f"simulated_{project['slug']}.json")


def connector_for(project):
    tracker = (project or {}).get("tracker", "none")
    if tracker == "github":
        return GitHubConnector(_env(project.get("token_env")), project.get("repo"),
                               _env(project.get("secret_env")))
    if tracker == "jira":
        connector = JiraConnector(project.get("base_url"), _env(project.get("email_env")),
                                  _env(project.get("token_env")), project.get("repo"),
                                  _env(project.get("secret_env")))
        connector.modules = project.get("modules")
        return connector
    if tracker == "simulated":
        return SimulatedConnector(simulated_path(project))
    return TrackerConnector()


def connector_signature(project):
    """What a connector is built from; when it changes, the connector is rebuilt."""
    keys = ("tracker", "repo", "base_url", "token_env", "email_env", "secret_env", "slug")
    return tuple(project.get(k) for k in keys) + tuple(_env(project.get(k))
                                                     for k in ("token_env", "email_env", "secret_env"))
