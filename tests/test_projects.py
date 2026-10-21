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


class ProjectConnectionTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["SIM_TRACKER_DIR"] = self.tmp.name
        self.second = db.create_project("second", "Second shop", "simulated")
        dev = db.get_user_by_username("dev.platform")
        db.set_membership(self.second, db.get_user_by_username("lead")["id"], "triage_lead")
        db.set_membership(self.second, dev["id"], "developer", ["payments", "api"])

    def tearDown(self):
        os.environ.pop("SIM_TRACKER_DIR", None)
        for k in ("GH_TOKEN_A", "GH_SECRET_A", "GH_TOKEN_B", "GH_SECRET_B"):
            os.environ.pop(k, None)
        super().tearDown()

    def sync(self, client, project=None):
        from unittest import mock
        from triage import llm
        url = "/api/integration/sync" + (f"?project={project}" if project else "")
        with mock.patch.object(llm, "call_model", return_value=llm_reply()):
            return client.post(url).get_json()

    # PJ6: two projects import the same tracker keys as separate issues and write back separately
    def test_pj6_two_projects_import_and_write_back_separately(self):
        lead = self.as_user("lead")
        self.assertEqual(self.sync(lead)["imported"], 4)
        self.assertEqual(self.sync(lead, "second")["imported"], 4)
        first_id = db.find_external("simulated", "SIM-2")
        second_id = db.find_external("simulated", "SIM-2", project_id=self.second)
        self.assertNotEqual(first_id, second_id)
        res = lead.post(f"/api/issues/{second_id}/triage",
                        json={"action": "accept", "assignee_id": db.get_user_by_username("dev.platform")["id"]})
        self.assertEqual(res.get_json()["sync_status"], "ok")
        pipelines = self.app.config["pipelines"]
        second_state = pipelines.get(self.second).connector.state()
        first_state = pipelines.default().connector.state()
        labelled = lambda state: [i["key"] for i in state["issues"] if i["labels"]]
        self.assertEqual(labelled(second_state), ["SIM-2"])
        self.assertEqual(labelled(first_state), [])
        self.assertEqual(db.get_project(self.second)["last_sync"] is not None, True)

    # PJ7: each project's webhook is verified with that project's own secret
    def test_pj7_webhook_per_project_secret(self):
        import hashlib, hmac, json
        from unittest import mock
        from triage import llm
        os.environ.update(GH_TOKEN_A="ta", GH_SECRET_A="secret-a", GH_TOKEN_B="tb", GH_SECRET_B="secret-b")
        db.create_project("repo-a", "acme/repo-a", "github", repo="acme/repo-a",
                          token_env="GH_TOKEN_A", secret_env="GH_SECRET_A")
        b = db.create_project("repo-b", "acme/repo-b", "github", repo="acme/repo-b",
                              token_env="GH_TOKEN_B", secret_env="GH_SECRET_B")
        client = self.app.test_client()
        payload = json.dumps({"action": "opened", "issue": {
            "number": 7, "title": "Login fails", "body": "Users cannot log in since the update.",
            "html_url": "https://github.com/acme/repo-b/issues/7", "user": {"login": "ann"}}}).encode()
        sign = lambda secret: "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
        wrong = client.post("/api/webhooks/repo-b", data=payload,
                            headers=headers | {"X-Hub-Signature-256": sign(b"secret-a")})
        self.assertEqual(wrong.status_code, 401)
        with mock.patch.object(llm, "call_model", return_value=llm_reply()):
            ok = client.post("/api/webhooks/repo-b", data=payload,
                             headers=headers | {"X-Hub-Signature-256": sign(b"secret-b")})
        self.assertTrue(ok.get_json()["handled"])
        self.assertIsNotNone(db.find_external("github", "7", project_id=b))
        self.assertEqual(client.post("/api/webhooks/no-such-project", data=payload).status_code, 404)

    # PJ8: a developer who is not a member of the project is not assigned in its tracker
    def test_pj8_non_member_not_assigned_in_tracker(self):
        lead = self.as_user("lead")
        self.sync(lead, "second")
        issue_id = db.find_external("simulated", "SIM-1", project_id=self.second)
        outsider = db.get_user_by_username("dev.frontend")["id"]
        lead.post(f"/api/issues/{issue_id}/triage", json={"action": "accept", "assignee_id": outsider})
        state = self.app.config["pipelines"].get(self.second).connector.state()
        self.assertIsNone(next(i for i in state["issues"] if i["key"] == "SIM-1")["assignee"])
        with db.connect() as conn:
            detail = conn.execute("SELECT detail FROM sync_events WHERE issue_id = ? AND action = 'triage'",
                                  (issue_id,)).fetchone()["detail"]
        self.assertIn("not a member of this project", detail)
