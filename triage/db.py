"""SQLite storage: users, issues (AI suggestions + human decisions), settings and audit log."""
import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,                  -- reporter | triage_lead | developer | admin
    password_hash TEXT NOT NULL,
    modules TEXT DEFAULT '[]',           -- developers only: modules they own (JSON list)
    github_login TEXT,                   -- for assigning issues in GitHub
    jira_account_id TEXT,                -- for assigning issues in Jira
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    reporter_id INTEGER REFERENCES users(id),
    assignee_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'new',  -- new | needs_info | assigned | resolved
    info_request TEXT,
    info_requested_at TEXT,
    source TEXT NOT NULL DEFAULT 'desk', -- desk | github | jira | simulated
    external_id TEXT, external_key TEXT, external_url TEXT, external_reporter TEXT,
    sync_status TEXT,                    -- ok | failed | pending | NULL (not linked)
    sync_error TEXT,
    sync_pending TEXT,                   -- outbox: JSON of the write-back still to deliver
    resolved_by INTEGER REFERENCES users(id),
    ai_type TEXT, ai_module TEXT, ai_priority TEXT,
    ai_confidence REAL, ai_justification TEXT,
    ai_source TEXT,                      -- llm | fallback | seed
    fallback_reason TEXT,
    needs_review INTEGER DEFAULT 0,
    review_reasons TEXT,
    duplicate_of INTEGER,
    recommendation TEXT,
    recommendation_sources TEXT,
    similar_json TEXT,
    redactions INTEGER DEFAULT 0,
    latency_ms INTEGER,
    tokens_in INTEGER, tokens_out INTEGER,
    triage_count INTEGER DEFAULT 0,
    final_type TEXT, final_module TEXT, final_priority TEXT,
    review_action TEXT,                  -- accept | override
    review_note TEXT,
    reporter_update TEXT,                -- approved message shown to the reporter
    resolution TEXT,
    is_seed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    resolved_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_external ON issues(source, external_id)
    WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    source TEXT NOT NULL,                -- agent | rules
    fallback_reason TEXT,
    plan_json TEXT NOT NULL,
    candidates_json TEXT,
    trace_json TEXT,
    status TEXT NOT NULL DEFAULT 'proposed', -- proposed | approved | edited | superseded
    decision_json TEXT,
    steps INTEGER, tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER,
    decided_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER,
    tracker TEXT NOT NULL,
    direction TEXT NOT NULL,             -- in | out
    action TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    issue_id INTEGER,
    detail TEXT,
    at TEXT NOT NULL
);
"""

# Demo accounts for the proof of concept. All use the password in DEMO_PASSWORD.
DEMO_PASSWORD = "demo1234"
DEMO_USERS = [
    ("admin", "System administrator", "admin", []),
    ("lead", "QA lead", "triage_lead", []),
    ("dev.payments", "Developer, payments and reporting", "developer", ["payments", "reporting"]),
    ("dev.platform", "Developer, platform and API", "developer",
     ["authentication", "api", "performance"]),
    ("dev.frontend", "Developer, front end", "developer", ["user_interface", "other"]),
    ("client.acme", "Acme Retail (client)", "reporter", []),
    ("tester", "Internal tester", "reporter", []),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    """Open a connection, commit (or roll back) on exit, and always close it.

    sqlite3's own context manager ends the transaction but leaves the connection open, which
    leaks a file handle per call. On Windows that handle locks the database file, so temporary
    test databases cannot be deleted. The inner `with conn` keeps the commit/rollback
    behaviour unchanged; the `finally` adds the close.
    """
    conn = sqlite3.connect(config.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(seed_csv=config.SEED_CSV):
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for username, name, role, modules in DEMO_USERS:
                conn.execute(
                    "INSERT INTO users (username, display_name, role, password_hash, modules, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (username, name, role, generate_password_hash(DEMO_PASSWORD),
                     json.dumps(modules), now()))
        if conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0 and seed_csv.exists():
            owner = {}
            for dev in conn.execute("SELECT id, modules FROM users WHERE role = 'developer'"):
                for module in json.loads(dev["modules"]):
                    owner.setdefault(module, dev["id"])
            with open(seed_csv, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    conn.execute(
                        """INSERT INTO issues (title, description, status, ai_source,
                           final_type, final_module, final_priority, resolution, resolved_by,
                           is_seed, created_at)
                           VALUES (?, ?, 'resolved', 'seed', ?, ?, ?, ?, ?, 1, ?)""",
                        (row["title"], row["description"], row["type"], row["module"],
                         row["priority"], row["resolution"], owner.get(row["module"]), now()))


# ---------- users ----------

def user_to_dict(row):
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d["modules"] = json.loads(d.get("modules") or "[]")
    d["active"] = bool(d["active"])
    return d


def get_user(user_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users(role=None):
    sql, params = "SELECT * FROM users", []
    if role:
        sql += " WHERE role = ?"
        params.append(role)
    with connect() as conn:
        return [user_to_dict(r) for r in conn.execute(sql + " ORDER BY role, username", params)]


def create_user(username, display_name, role, password, modules):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role, password_hash, modules, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (username, display_name, role, generate_password_hash(password),
             json.dumps(modules), now()))
        return cur.lastrowid


def update_user(user_id, **fields):
    allowed = {"display_name", "role", "active", "modules", "password", "github_login",
               "jira_account_id"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "password":
            sets.append("password_hash = ?")
            params.append(generate_password_hash(value))
        elif key == "modules":
            sets.append("modules = ?")
            params.append(json.dumps(value))
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if sets:
        with connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params + [user_id])


def developer_workload():
    """Active developers with their modules and number of open assigned issues."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.*, (SELECT COUNT(*) FROM issues i
                            WHERE i.assignee_id = u.id AND i.status = 'assigned') AS open_count
               FROM users u WHERE u.role = 'developer' AND u.active = 1""").fetchall()
    out = []
    for r in rows:
        d = user_to_dict(r)
        d["open_count"] = r["open_count"]
        out.append(d)
    return out


# ---------- issues ----------

AI_COLUMNS = ["ai_type", "ai_module", "ai_priority", "ai_confidence", "ai_justification",
              "ai_source", "fallback_reason", "needs_review", "review_reasons", "duplicate_of",
              "recommendation", "recommendation_sources", "similar_json", "redactions",
              "latency_ms", "tokens_in", "tokens_out"]


EXTERNAL_COLUMNS = ["source", "external_id", "external_key", "external_url", "external_reporter"]


def insert_issue(title, description, reporter_id, ai: dict, external: dict = None) -> int:
    external = external or {"source": "desk"}
    cols = ["title", "description", "reporter_id"] + AI_COLUMNS + EXTERNAL_COLUMNS
    values = ([title, description, reporter_id] + [ai.get(c) for c in AI_COLUMNS]
              + [external.get(c) for c in EXTERNAL_COLUMNS])
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO issues ({', '.join(cols)}, triage_count, status, created_at) "
            f"VALUES ({', '.join('?' * len(cols))}, 1, 'new', ?)", values + [now()])
        return cur.lastrowid


def update_after_retriage(issue_id, description, ai: dict):
    sets = ", ".join(f"{c} = ?" for c in AI_COLUMNS)
    with connect() as conn:
        conn.execute(
            f"UPDATE issues SET description = ?, {sets}, status = 'new', info_request = NULL, "
            f"triage_count = triage_count + 1 WHERE id = ?",
            [description] + [ai.get(c) for c in AI_COLUMNS] + [issue_id])


ISSUE_SELECT = """SELECT i.*, COALESCE(r.display_name, i.external_reporter) AS reporter_name,
                         a.display_name AS assignee_name, a.github_login AS assignee_github,
                         a.jira_account_id AS assignee_jira
                  FROM issues i
                  LEFT JOIN users r ON r.id = i.reporter_id
                  LEFT JOIN users a ON a.id = i.assignee_id"""


def get_issue(issue_id):
    with connect() as conn:
        row = conn.execute(ISSUE_SELECT + " WHERE i.id = ?", (issue_id,)).fetchone()
        return dict(row) if row else None


def list_issues(status=None, reporter_id=None, assignee_id=None):
    sql, params = ISSUE_SELECT + " WHERE i.is_seed = 0", []
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if reporter_id is not None:
        sql += " AND i.reporter_id = ?"
        params.append(reporter_id)
    if assignee_id is not None:
        sql += " AND i.assignee_id = ?"
        params.append(assignee_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def save_triage_decision(issue_id, action, final_type, final_module, final_priority,
                         assignee_id, note, reporter_update=None):
    with connect() as conn:
        conn.execute(
            """UPDATE issues SET status = 'assigned', review_action = ?, final_type = ?,
               final_module = ?, final_priority = ?, assignee_id = ?, review_note = ?,
               reporter_update = ?, reviewed_at = ? WHERE id = ?""",
            (action, final_type, final_module, final_priority, assignee_id, note,
             reporter_update, now(), issue_id))


def request_info(issue_id, message):
    with connect() as conn:
        conn.execute("UPDATE issues SET status = 'needs_info', info_request = ?, "
                     "info_requested_at = ? WHERE id = ?", (message, now(), issue_id))


def save_resolution(issue_id, resolution, resolved_by=None):
    with connect() as conn:
        conn.execute("UPDATE issues SET status = 'resolved', resolution = ?, resolved_at = ?, "
                     "resolved_by = ? WHERE id = ?", (resolution, now(), resolved_by, issue_id))


def find_external(source, external_id):
    with connect() as conn:
        row = conn.execute("SELECT id FROM issues WHERE source = ? AND external_id = ?",
                           (source, str(external_id))).fetchone()
        return row["id"] if row else None


SYNC_FIELDS = {"sync_status", "sync_error", "sync_pending"}


def set_sync(issue_id, **fields):
    sets = [f"{k} = ?" for k in fields if k in SYNC_FIELDS]
    with connect() as conn:
        conn.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id = ?",
                     [v for k, v in fields.items() if k in SYNC_FIELDS] + [issue_id])


def linked_issues(source, status):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM issues WHERE source = ? AND status = ?",
                            (source, status)).fetchall()
        return [dict(r) for r in rows]


def failed_syncs():
    with connect() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM issues WHERE sync_pending IS NOT NULL")]


# ---------- developer history (used for assignee ranking) ----------

def resolvers_for(issue_ids):
    """{developer_id: [issue ids they resolved]} for the given past issues."""
    if not issue_ids:
        return {}
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, resolved_by FROM issues WHERE resolved_by IS NOT NULL AND id IN "
            f"({', '.join('?' * len(issue_ids))})", list(issue_ids)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["resolved_by"], []).append(r["id"])
    return out


def module_fix_counts(module):
    """{developer_id: number of resolved issues in this module}."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT resolved_by, COUNT(*) AS n FROM issues WHERE status = 'resolved' AND "
            "resolved_by IS NOT NULL AND COALESCE(final_module, ai_module) = ? GROUP BY resolved_by",
            (module,)).fetchall()
    return {r["resolved_by"]: r["n"] for r in rows}


# ---------- agent plans ----------

def insert_plan(issue_id, source, plan, candidates, trace, fallback_reason=None, usage=None):
    usage = usage or {}
    with connect() as conn:
        conn.execute("UPDATE agent_plans SET status = 'superseded' "
                     "WHERE issue_id = ? AND status = 'proposed'", (issue_id,))
        cur = conn.execute(
            """INSERT INTO agent_plans (issue_id, source, fallback_reason, plan_json,
               candidates_json, trace_json, steps, tokens_in, tokens_out, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (issue_id, source, fallback_reason, json.dumps(plan), json.dumps(candidates),
             json.dumps(trace), usage.get("steps"), usage.get("input_tokens"),
             usage.get("output_tokens"), usage.get("latency_ms"), now()))
        return cur.lastrowid


def latest_plan(issue_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent_plans WHERE issue_id = ? AND status != 'superseded' "
                           "ORDER BY id DESC LIMIT 1", (issue_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("plan_json", "candidates_json", "trace_json", "decision_json"):
        d[key.replace("_json", "")] = json.loads(d.pop(key) or "null")
    return d


def decide_plan(plan_id, status, decision, user_id):
    with connect() as conn:
        conn.execute("UPDATE agent_plans SET status = ?, decision_json = ?, decided_by = ?, "
                     "decided_at = ? WHERE id = ?",
                     (status, json.dumps(decision), user_id, now(), plan_id))


def plan_rows():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agent_plans")]


# ---------- sync log ----------

def log_sync(issue_id, tracker, direction, action, ok, detail=None):
    with connect() as conn:
        conn.execute("INSERT INTO sync_events (issue_id, tracker, direction, action, ok, detail, at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (issue_id, tracker, direction, action, int(ok), detail, now()))


def sync_events(limit=100):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sync_events ORDER BY id DESC LIMIT ?", (limit,))]


def knowledge_base():
    """Resolved issues with a resolution: used for similarity search and recommendations."""
    with connect() as conn:
        rows = conn.execute("SELECT id, title, description, resolution FROM issues "
                            "WHERE status = 'resolved' AND resolution IS NOT NULL").fetchall()
        return [dict(r) for r in rows]


def labelled_issues():
    """Issues with human-confirmed labels: used to train the fallback classifier."""
    with connect() as conn:
        rows = conn.execute("SELECT title, description, final_type, final_module, final_priority "
                            "FROM issues WHERE final_type IS NOT NULL").fetchall()
        return [dict(r) for r in rows]


# ---------- settings and audit ----------

def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def audit(user_id, action, issue_id=None, detail=None):
    with connect() as conn:
        conn.execute("INSERT INTO audit_log (user_id, action, issue_id, detail, at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (user_id, action, issue_id, json.dumps(detail) if detail else None, now()))


def audit_entries(limit=100):
    with connect() as conn:
        rows = conn.execute(
            """SELECT l.*, u.display_name AS user_name, u.role AS user_role FROM audit_log l
               LEFT JOIN users u ON u.id = l.user_id ORDER BY l.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]
