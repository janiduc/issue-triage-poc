"""Multi-project support: data model, migration and isolation of imported issue ids."""
import os
import sqlite3

from tests.test_triage import Base, llm_reply
from triage import db


class ProjectModelTests(Base):
    # PJ1: the first project is created from .env and owns every issue and non-admin user
    def test_pj1_default_project_and_memberships(self):
        projects = db.list_projects()
        self.assertEqual([p["slug"] for p in projects], ["simulated"])
        pid = projects[0]["id"]
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM issues WHERE project_id IS NULL").fetchone()[0], 0)
        lead = db.get_user_by_username("lead")
        admin = db.get_user_by_username("admin")
        self.assertEqual(db.memberships(lead["id"]), {pid: {"role": "triage_lead", "modules": []}})
        self.assertEqual(db.memberships(admin["id"]), {})  # admin stays global
        dev = db.get_user_by_username("dev.payments")
        self.assertEqual(db.memberships(dev["id"])[pid]["modules"], ["payments", "reporting"])

    # PJ2: running the migration again changes nothing
    def test_pj2_migration_is_idempotent(self):
        def counts():
            with db.connect() as conn:
                return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                        for t in ("projects", "project_members", "issues")]
        before = counts()
        db.init_db()
        db.init_db()
        self.assertEqual(counts(), before)

    # PJ3: the same tracker issue number can exist in two projects
    def test_pj3_external_ids_are_unique_per_project(self):
        other = db.create_project("second-repo", "acme/second-repo", "github", repo="acme/second-repo")
        ext = {"source": "github", "external_id": "1", "external_key": "#1", "external_url": "u",
               "external_reporter": "x"}
        ai = {"ai_type": "bug", "ai_module": "api", "ai_priority": "low", "ai_confidence": 0.5}
        a = db.insert_issue("A", "first repo issue", None, ai, ext)
        b = db.insert_issue("B", "second repo issue", None, ai, ext | {"project_id": other})
        self.assertNotEqual(a, b)
        self.assertEqual(db.find_external("github", "1"), a)
        self.assertEqual(db.find_external("github", "1", project_id=other), b)
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert_issue("C", "duplicate in the same project", None, ai, ext | {"project_id": other})

    # PJ4: records created by the app carry their issue's project
    def test_pj4_plans_audit_and_sync_events_carry_the_project(self):
        lead = self.as_user("lead")
        issue = self.submit(lead, "Checkout error", "Checkout fails with an error for all customers.",
                            llm_reply()).get_json()
        pid = db.get_issue(issue["id"])["project_id"]
        with db.connect() as conn:
            plan = conn.execute("SELECT project_id FROM agent_plans WHERE issue_id = ?", (issue["id"],)).fetchone()
            audit = conn.execute("SELECT project_id FROM audit_log WHERE issue_id = ?", (issue["id"],)).fetchone()
        self.assertEqual(plan["project_id"], pid)
        self.assertEqual(audit["project_id"], pid)

    # PJ5: a database created before projects existed is migrated on start-up
    def test_pj5_migrates_a_pre_project_database(self):
        path = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(path)
        old_schema = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS projects")[0]  # tables as they were
        conn.executescript(old_schema + "\nCREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, "
                           "user_id INTEGER, action TEXT NOT NULL, issue_id INTEGER, detail TEXT, at TEXT NOT NULL);"
                           "\nCREATE UNIQUE INDEX ux_external ON issues(source, external_id) WHERE external_id IS NOT NULL;")
        conn.execute("INSERT INTO users (username, display_name, role, password_hash, modules, created_at) "
                     "VALUES ('old.dev', 'Old developer', 'developer', 'x', '[\"api\"]', 'now')")
        conn.execute("INSERT INTO issues (title, description, source, external_id, created_at) "
                     "VALUES ('Old issue', 'from before projects', 'simulated', 'SIM-9', 'now')")
        conn.execute("INSERT INTO settings (key, value) VALUES ('sync_since_simulated', '2026-10-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        os.environ["DB_PATH"] = path
        db.init_db()
        project = db.list_projects()[0]
        self.assertEqual(project["sync_since"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(db.find_external("simulated", "SIM-9"), 1)
        old_dev = db.get_user_by_username("old.dev")
        self.assertEqual(db.memberships(old_dev["id"])[project["id"]], {"role": "developer", "modules": ["api"]})
