"""SQLite storage: users, issues (AI suggestions + human decisions), settings and audit log."""
import csv
import json
import os
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

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,           -- used in URLs, e.g. /api/webhooks/<slug>
    name TEXT NOT NULL,
    tracker TEXT NOT NULL DEFAULT 'none', -- github | jira | simulated | none
    repo TEXT,                           -- owner/name (GitHub) or project key (Jira)
    base_url TEXT,                       -- Jira site URL
    token_env TEXT,                      -- NAME of the env variable holding the token
    email_env TEXT,                      -- NAME of the env variable holding the Jira email
    secret_env TEXT,                     -- NAME of the env variable holding the webhook secret
    modules TEXT NOT NULL,               -- JSON list of this project's modules
    active INTEGER NOT NULL DEFAULT 1,
    managed_by_env INTEGER NOT NULL DEFAULT 0, -- 1: tracker settings follow .env (the first project)
    share_seed INTEGER NOT NULL DEFAULT 0, -- 1: also learn from the generic seeded fixes
    last_sync TEXT,
    sync_since TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,                  -- triage_lead | developer | reporter
    modules TEXT DEFAULT '[]',           -- developers: modules they own in this project
    added_at TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id)
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


PROJECT_COLUMNS = ("issues", "agent_plans", "sync_events", "audit_log")


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate_projects(conn):
    """Bring a database to the multi-project shape. Safe to run on every start.

    - adds project_id to issues, plans, sync events and audit entries;
    - imported issues are unique per project, not globally (issue #1 can exist in two repos);
    - creates the first project from today's .env when there is none;
    - gives that project every issue without one, and every non-admin user a membership with
      their current role and modules (an admin needs no membership: admin stays global);
    - moves the old per-tracker sync state onto the project.
    """
    if "share_seed" not in _columns(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN share_seed INTEGER NOT NULL DEFAULT 0")
    if "managed_by_env" not in _columns(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN managed_by_env INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE projects SET managed_by_env = 1 WHERE id = (SELECT MIN(id) FROM projects)")
    for table in PROJECT_COLUMNS:
        if "project_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id INTEGER REFERENCES projects(id)")
    conn.execute("DROP INDEX IF EXISTS ux_external")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_project_external "
                 "ON issues(project_id, source, external_id) WHERE external_id IS NOT NULL")
    if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
        p = config.default_project_settings()
        conn.execute(
            "INSERT INTO projects (slug, name, tracker, repo, base_url, token_env, email_env, "
            "secret_env, modules, last_sync, sync_since, created_at, managed_by_env) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (p["slug"], p["name"], p["tracker"], p.get("repo"), p.get("base_url"), p.get("token_env"),
             p.get("email_env"), p.get("secret_env"), json.dumps(config.MODULES),
             _setting(conn, f"last_sync_{p['tracker']}"), _setting(conn, f"sync_since_{p['tracker']}"),
             now()))
    default = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]
    for table in PROJECT_COLUMNS:
        if table == "audit_log":
            continue  # entries without an issue (sign-ins, settings) belong to no project
        conn.execute(f"UPDATE {table} SET project_id = ? WHERE project_id IS NULL", (default,))
    conn.execute("UPDATE audit_log SET project_id = (SELECT project_id FROM issues WHERE id = audit_log.issue_id) "
                 "WHERE project_id IS NULL AND issue_id IS NOT NULL")
    conn.execute(
        "INSERT OR IGNORE INTO project_members (project_id, user_id, role, modules, added_at) "
        "SELECT ?, id, role, modules, ? FROM users WHERE role != 'admin'", (default, now()))


def _setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def init_db(seed_csv=config.SEED_CSV):
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            if config.seed_demo_users():
                users = [(u, n, r, m, DEMO_PASSWORD) for u, n, r, m in DEMO_USERS]
            else:
                # No demo accounts: create a single administrator from the environment.
                username = os.getenv("ADMIN_USERNAME", "admin").strip().lower()
                password = os.getenv("ADMIN_PASSWORD") or ""
                if len(password) < 12:
                    raise RuntimeError("Without demo accounts, set ADMIN_PASSWORD (12+ characters) "
                                       "for the first administrator.")
                users = [(username, "System administrator", "admin", [], password)]
            for username, name, role, modules, password in users:
                conn.execute(
                    "INSERT INTO users (username, display_name, role, password_hash, modules, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (username, name, role, generate_password_hash(password),
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
        migrate_projects(conn)


# ---------- projects ----------

def project_to_dict(row):
    if not row:
        return None
    d = dict(row)
    d["modules"] = json.loads(d.get("modules") or "[]")
    d["active"] = bool(d["active"])
    d["share_seed"] = bool(d.get("share_seed"))
    return d


def default_project_id():
    """Until the desk is project-aware end to end, single-project code paths use the first project."""
    with connect() as conn:
        row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        return row["id"] if row else None


def get_project(project_id):
    with connect() as conn:
        return project_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())


def get_project_by_slug(slug):
    with connect() as conn:
        return project_to_dict(conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone())


def list_projects(active_only=False):
    sql = "SELECT * FROM projects" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    with connect() as conn:
        return [project_to_dict(r) for r in conn.execute(sql)]


def create_project(slug, name, tracker="none", modules=None, **fields):
    allowed = {"repo", "base_url", "token_env", "email_env", "secret_env"}
    extra = {k: v for k, v in fields.items() if k in allowed}
    cols = ["slug", "name", "tracker", "modules", "created_at"] + list(extra)
    values = [slug, name, tracker, json.dumps(modules or config.MODULES), now()] + list(extra.values())
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO projects ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           values)
        return cur.lastrowid


PROJECT_FIELDS = {"name", "tracker", "repo", "base_url", "token_env", "email_env", "secret_env",
                  "modules", "active", "last_sync", "sync_since", "managed_by_env", "share_seed"}


def update_project(project_id, **fields):
    sets, params = [], []
    for key, value in fields.items():
        if key not in PROJECT_FIELDS:
            continue
        sets.append(f"{key} = ?")
        params.append(json.dumps(value) if key == "modules" else value)
    if sets:
        with connect() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params + [project_id])


def sync_env_project():
    """The first project's tracker settings follow .env until an administrator edits it."""
    with connect() as conn:
        row = conn.execute("SELECT id FROM projects WHERE managed_by_env = 1 ORDER BY id LIMIT 1").fetchone()
    if not row:
        return
    p = config.default_project_settings()
    update_project(row["id"], tracker=p["tracker"], repo=p.get("repo"), base_url=p.get("base_url"),
                   token_env=p.get("token_env"), email_env=p.get("email_env"),
                   secret_env=p.get("secret_env"))


def set_membership(project_id, user_id, role, modules=()):
    with connect() as conn:
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role, modules, added_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role, modules = excluded.modules",
            (project_id, user_id, role, json.dumps(list(modules)), now()))


def remove_membership(project_id, user_id):
    with connect() as conn:
        conn.execute("DELETE FROM project_members WHERE project_id = ? AND user_id = ?", (project_id, user_id))


def memberships(user_id):
    """{project_id: {"role", "modules"}} for one user."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM project_members WHERE user_id = ?", (user_id,)).fetchall()
    return {r["project_id"]: {"role": r["role"], "modules": json.loads(r["modules"] or "[]")} for r in rows}


def project_members(project_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT m.role AS project_role, m.modules AS project_modules, m.added_at, u.* "
            "FROM project_members m JOIN users u ON u.id = m.user_id WHERE m.project_id = ? "
            "ORDER BY m.role, u.username", (project_id,)).fetchall()
    out = []
    for r in rows:
        d = user_to_dict(r)
        d["project_role"] = r["project_role"]
        d["project_modules"] = json.loads(r["project_modules"] or "[]")
        out.append(d)
    return out


def project_counts(project_id):
    """Numbers only (the administrator never sees issue content)."""
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM issues WHERE project_id = ? AND is_seed = 0 "
                            "GROUP BY status", (project_id,)).fetchall()
        members = conn.execute("SELECT COUNT(*) FROM project_members WHERE project_id = ?",
                               (project_id,)).fetchone()[0]
    return {"by_status": {r["status"]: r["n"] for r in rows}, "members": members}


def open_assignments(project_id, user_id):
    with connect() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM issues WHERE project_id = ? AND assignee_id = ? AND status = 'assigned'",
            (project_id, user_id))]


def release_assignments(project_id, user_id):
    """Send a developer's open issues in a project back to the lead's queue."""
    with connect() as conn:
        conn.execute("UPDATE issues SET status = 'new', assignee_id = NULL WHERE project_id = ? "
                     "AND assignee_id = ? AND status = 'assigned'", (project_id, user_id))


def _sync_default_membership(user_id):
    """Phase 1 compatibility: a user's global role and modules mirror into the first project."""
    user = get_user(user_id)
    project_id = default_project_id()
    if not user or project_id is None:
        return
    if user["role"] == "admin":
        remove_membership(project_id, user_id)
    else:
        set_membership(project_id, user_id, user["role"], json.loads(user["modules"] or "[]"))


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
        user_id = cur.lastrowid
    _sync_default_membership(user_id)
    return user_id


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
        _sync_default_membership(user_id)


def developer_workload(project_id=None):
    """A project's active developers with the modules they own there and their open assignments
    in that project."""
    if project_id is None:
        project_id = default_project_id()
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.*, m.modules AS project_modules,
                      (SELECT COUNT(*) FROM issues i WHERE i.assignee_id = u.id
                       AND i.status = 'assigned' AND i.project_id = m.project_id) AS open_count
               FROM project_members m JOIN users u ON u.id = m.user_id
               WHERE m.project_id = ? AND m.role = 'developer' AND u.active = 1""",
            (project_id,)).fetchall()
    out = []
    for r in rows:
        d = user_to_dict(r)
        d["modules"] = json.loads(r["project_modules"] or "[]")
        d["open_count"] = r["open_count"]
        out.append(d)
    return out


# ---------- issues ----------

AI_COLUMNS = ["ai_type", "ai_module", "ai_priority", "ai_confidence", "ai_justification",
              "ai_source", "fallback_reason", "needs_review", "review_reasons", "duplicate_of",
              "recommendation", "recommendation_sources", "similar_json", "redactions",
              "latency_ms", "tokens_in", "tokens_out"]


EXTERNAL_COLUMNS = ["source", "external_id", "external_key", "external_url", "external_reporter",
                    "project_id"]


def insert_issue(title, description, reporter_id, ai: dict, external: dict = None) -> int:
    external = dict(external or {"source": "desk"})
    external.setdefault("project_id", default_project_id())
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


def list_issues(status=None, reporter_id=None, assignee_id=None, project_id=None):
    sql, params = ISSUE_SELECT + " WHERE i.is_seed = 0", []
    if project_id is not None:
        sql += " AND i.project_id = ?"
        params.append(project_id)
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


def find_external(source, external_id, project_id=None):
    if project_id is None:
        project_id = default_project_id()
    with connect() as conn:
        row = conn.execute("SELECT id FROM issues WHERE project_id = ? AND source = ? AND external_id = ?",
                           (project_id, source, str(external_id))).fetchone()
        return row["id"] if row else None


SYNC_FIELDS = {"sync_status", "sync_error", "sync_pending"}


def set_sync(issue_id, **fields):
    sets = [f"{k} = ?" for k in fields if k in SYNC_FIELDS]
    with connect() as conn:
        conn.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id = ?",
                     [v for k, v in fields.items() if k in SYNC_FIELDS] + [issue_id])


def linked_issues(source, status, project_id=None):
    if project_id is None:
        project_id = default_project_id()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM issues WHERE project_id = ? AND source = ? AND status = ?",
                            (project_id, source, status)).fetchall()
        return [dict(r) for r in rows]


def failed_syncs(project_id=None):
    """Issues with tracker writes still to deliver; all projects when project_id is None."""
    sql, params = "SELECT id FROM issues WHERE sync_pending IS NOT NULL", []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    with connect() as conn:
        return [r["id"] for r in conn.execute(sql, params)]


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


def module_fix_counts(module, project_id=None):
    """{developer_id: number of resolved issues in this module, within the project}."""
    if project_id is None:
        project_id = default_project_id()
    with connect() as conn:
        rows = conn.execute(
            "SELECT resolved_by, COUNT(*) AS n FROM issues WHERE status = 'resolved' AND "
            "resolved_by IS NOT NULL AND COALESCE(final_module, ai_module) = ? AND project_id = ? "
            "GROUP BY resolved_by", (module, project_id)).fetchall()
    return {r["resolved_by"]: r["n"] for r in rows}


# ---------- agent plans ----------

def insert_plan(issue_id, source, plan, candidates, trace, fallback_reason=None, usage=None):
    usage = usage or {}
    with connect() as conn:
        conn.execute("UPDATE agent_plans SET status = 'superseded' "
                     "WHERE issue_id = ? AND status = 'proposed'", (issue_id,))
        cur = conn.execute(
            """INSERT INTO agent_plans (issue_id, source, fallback_reason, plan_json,
               candidates_json, trace_json, steps, tokens_in, tokens_out, latency_ms, created_at,
               project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, (SELECT project_id FROM issues WHERE id = ?))""",
            (issue_id, source, fallback_reason, json.dumps(plan), json.dumps(candidates),
             json.dumps(trace), usage.get("steps"), usage.get("input_tokens"),
             usage.get("output_tokens"), usage.get("latency_ms"), now(), issue_id))
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


def plan_rows(project_id=None):
    sql, params = "SELECT * FROM agent_plans", []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ---------- sync log ----------

def log_sync(issue_id, tracker, direction, action, ok, detail=None, project_id=None):
    if project_id is None:
        project_id = (get_issue(issue_id) or {}).get("project_id") if issue_id else default_project_id()
    with connect() as conn:
        conn.execute("INSERT INTO sync_events (issue_id, tracker, direction, action, ok, detail, at, project_id)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (issue_id, tracker, direction, action, int(ok), detail, now(), project_id))


def sync_events(limit=100, project_id=None):
    sql, params = "SELECT * FROM sync_events", []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + [limit])]


def knowledge_base(project_id=None, include_seed=False):
    """Resolved issues with a resolution, for similarity search and recommendations. Per project:
    one project's fixes are never suggested in another. include_seed adds the generic seeded fixes
    (they live in the first project) for a new project that opted in."""
    sql = ("SELECT id, title, description, resolution FROM issues "
           "WHERE status = 'resolved' AND resolution IS NOT NULL")
    params = []
    if project_id is not None:
        sql += " AND (project_id = ?" + (" OR is_seed = 1)" if include_seed else ")")
        params.append(project_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def labelled_issues(project_id=None, seed_only=False):
    """Issues with human-confirmed labels: used to train the fallback classifier."""
    sql, params = ("SELECT title, description, final_type, final_module, final_priority "
                   "FROM issues WHERE final_type IS NOT NULL"), []
    if seed_only:
        sql += " AND is_seed = 1"
    elif project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


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
        conn.execute("INSERT INTO audit_log (user_id, action, issue_id, detail, at, project_id) "
                     "VALUES (?, ?, ?, ?, ?, (SELECT project_id FROM issues WHERE id = ?))",
                     (user_id, action, issue_id, json.dumps(detail) if detail else None, now(), issue_id))


def audit_entries(limit=100):
    with connect() as conn:
        rows = conn.execute(
            """SELECT l.*, u.display_name AS user_name, u.role AS user_role FROM audit_log l
               LEFT JOIN users u ON u.id = l.user_id ORDER BY l.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]
