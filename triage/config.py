"""Central configuration. Values can be overridden with environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Allowed labels. Keep these in sync with the seed data and the LLM prompt.
ISSUE_TYPES = ["bug", "feature_request", "question", "task"]
MODULES = ["authentication", "payments", "reporting", "user_interface",
           "performance", "api", "other"]
PRIORITIES = ["critical", "high", "medium", "low"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}  # critical sorts first

# Triage behaviour
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))
MIN_DESCRIPTION_WORDS = int(os.getenv("MIN_DESCRIPTION_WORDS", "6"))
TOP_K_SIMILAR = 3


def db_path() -> str:
    return os.getenv("DB_PATH", str(BASE_DIR / "triage.db"))


SEED_CSV = BASE_DIR / "data" / "seed_resolved_issues.csv"
EVAL_CSV = BASE_DIR / "data" / "eval_issues.csv"


# LLM settings are read at call time so tests can switch them on and off.
def llm_api_key():
    return os.getenv("ANTHROPIC_API_KEY")


def llm_enabled() -> bool:
    return os.getenv("LLM_ENABLED", "true").lower() == "true" and bool(llm_api_key())


def llm_model() -> str:
    # A small, low-cost model is usually enough for triage; change to compare models.
    return os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")


def llm_timeout() -> float:
    return float(os.getenv("LLM_TIMEOUT", "20"))


def use_embeddings() -> bool:
    return os.getenv("USE_EMBEDDINGS", "true").lower() == "true"


# User roles, in the order they appear in the admin screen.
ROLES = ["reporter", "triage_lead", "developer", "admin"]
ROLE_LABELS = {"reporter": "Reporter", "triage_lead": "Triage lead",
               "developer": "Developer", "admin": "Administrator"}
ISSUE_STATUSES = ["new", "needs_info", "assigned", "resolved"]


def secret_key() -> str:
    # Must be set to a long random value outside local demos.
    return os.getenv("SECRET_KEY", "dev-only-change-me")
