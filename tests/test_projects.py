"""Multi-project support: data model, migration and isolation of imported issue ids."""
import os
import sqlite3

from app import create_app
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

    # PJ8: only the project's own developers can be assigned, in the desk and in its tracker
    def test_pj8_only_project_developers_are_assigned(self):
        lead = self.as_user("lead")
        self.sync(lead, "second")
        issue_id = db.find_external("simulated", "SIM-1", project_id=self.second)
        outsider = db.get_user_by_username("dev.frontend")["id"]
        res = lead.post(f"/api/issues/{issue_id}/triage", json={"action": "accept", "assignee_id": outsider})
        self.assertEqual(res.status_code, 400)
        self.assertIn("developer of this project", res.get_json()["error"])
        # the tracker write also refuses an assignee who is not a member
        db.save_triage_decision(issue_id, "accept", "bug", "api", "low", outsider, None)
        write = self.app.config["pipelines"].get(self.second).triage_write(db.get_issue(issue_id), [], None)
        self.assertIsNone(write["assignee"])
        self.assertIn("not a member of this project", write["warning"])


class ProjectAdminTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = self.as_user("admin")

    def create(self, **overrides):
        data = {"slug": "booking", "name": "Booking API", "tracker": "simulated",
                "modules": ["bookings", "payments", "notifications"]} | overrides
        return self.admin.post("/api/admin/projects", json=data)

    def uid(self, username):
        return db.get_user_by_username(username)["id"]

    # PM1: the administrator creates projects; bad input and secrets in place of names are refused
    def test_pm1_create_project_and_validation(self):
        res = self.create()
        self.assertEqual(res.status_code, 201, res.get_json())
        self.assertEqual(res.get_json()["modules"], ["bookings", "payments", "notifications"])
        self.assertEqual(self.create().status_code, 400)                          # duplicate slug
        self.assertEqual(self.create(slug="Bad Slug!").status_code, 400)
        self.assertEqual(self.create(slug="gh", tracker="github", repo="no-slash").status_code, 400)
        token_value = "ghp_" + "x" * 36
        res = self.create(slug="gh2", tracker="github", repo="acme/shop", token_env=token_value)
        self.assertEqual(res.status_code, 400)                                     # a secret, not a name
        self.assertIn("environment variable name", res.get_json()["error"])
        self.assertEqual(self.as_user("lead").get("/api/admin/projects").status_code, 403)
        listed = self.admin.get("/api/admin/projects").get_json()
        self.assertEqual([p["slug"] for p in listed], ["simulated", "booking"])
        self.assertNotIn("title", str(listed))                                    # numbers only, no issue text

    # PM2: add, change and remove members; the user and their other projects are untouched
    def test_pm2_members_add_change_remove(self):
        self.create()
        dev = self.uid("dev.frontend")
        res = self.admin.post("/api/admin/projects/booking/members",
                              json={"user_id": dev, "role": "developer", "modules": ["bookings"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.admin.post("/api/admin/projects/booking/members",
                                         json={"user_id": self.uid("admin"), "role": "reporter"}).status_code, 400)
        self.assertEqual(self.admin.post("/api/admin/projects/booking/members",
                                         json={"user_id": dev, "role": "developer",
                                               "modules": ["payments"]}).status_code, 400)  # already a member
        res = self.admin.patch(f"/api/admin/projects/booking/members/{dev}", json={"role": "reporter"})
        self.assertEqual([m["role"] for m in res.get_json()], ["reporter"])
        res = self.admin.delete(f"/api/admin/projects/booking/members/{dev}")
        self.assertEqual(res.get_json(), [])
        self.assertIsNotNone(db.get_user(dev))
        self.assertIn(db.default_project_id(), db.memberships(dev))               # other project kept
        actions = [e["action"] for e in self.admin.get("/api/admin/audit").get_json()]
        for action in ("project_created", "member_added", "member_role_changed", "member_removed"):
            self.assertIn(action, actions)

    # PM3: the last lead is protected; a developer with open work is released before removal
    def test_pm3_removal_safeguards(self):
        pid = db.default_project_id()
        lead = self.uid("lead")
        res = self.admin.delete(f"/api/admin/projects/simulated/members/{lead}")
        self.assertEqual(res.status_code, 400)
        self.assertIn("only triage lead", res.get_json()["error"])
        lead_client = self.as_user("lead")
        issue = self.submit(lead_client, "Refund missing", "A refund did not appear in the account.").get_json()
        dev = self.uid("dev.payments")
        lead_client.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept", "assignee_id": dev})
        res = self.admin.delete(f"/api/admin/projects/simulated/members/{dev}")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["open_issue_count"], 1)
        res = self.admin.delete(f"/api/admin/projects/simulated/members/{dev}?release=true")
        self.assertEqual(res.status_code, 200)
        released = db.get_issue(issue["id"])
        self.assertEqual((released["status"], released["assignee_id"]), ("new", None))
        self.assertNotIn(pid, db.memberships(dev))

    # PM4: test connection reports assignable users and members GitHub cannot assign
    def test_pm4_test_connection(self):
        from unittest import mock
        from tests.test_agent_pipeline import FakeResponse
        from triage.integrations import http as tracker_http
        os.environ["GH_TOKEN_SHOP"] = "t"
        try:
            self.create(slug="shop", tracker="github", repo="acme/shop", token_env="GH_TOKEN_SHOP")
            dev = self.uid("dev.platform")
            db.update_user(dev, github_login="platform-dev")
            self.admin.post("/api/admin/projects/shop/members", json={"user_id": dev, "role": "developer"})

            def fake(method, url, **kw):
                return FakeResponse(200, [{"login": "someone-else"}] if url.endswith("/assignees") else {"id": 1})
            with mock.patch.object(tracker_http.requests, "request", side_effect=fake):
                res = self.admin.post("/api/admin/projects/shop/test").get_json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["unassignable"], ["dev.platform"])
            os.environ.pop("GH_TOKEN_SHOP")
            res = self.admin.post("/api/admin/projects/shop/test").get_json()     # token variable now empty
            self.assertFalse(res["ok"])
        finally:
            os.environ.pop("GH_TOKEN_SHOP", None)

    # PM5: editing the first project's tracker stops it following .env; archiving deactivates it
    def test_pm5_edit_and_archive(self):
        res = self.admin.patch("/api/admin/projects/simulated", json={"tracker": "none"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(db.get_project_by_slug("simulated")["managed_by_env"], 0)
        db.sync_env_project()
        self.assertEqual(db.get_project_by_slug("simulated")["tracker"], "none")   # no longer overwritten
        self.create()
        self.admin.patch("/api/admin/projects/booking", json={"active": False})
        self.assertFalse(db.get_project_by_slug("booking")["active"])


class ProjectAccessTests(Base):
    """Phase 4: every permission is the user's role in the issue's project."""

    def setUp(self):
        super().setUp()
        self.admin = self.as_user("admin")
        self.admin.post("/api/admin/projects", json={"slug": "booking", "name": "Booking API", "tracker": "none",
                                                     "modules": ["bookings", "payments"]})
        self.booking = db.get_project_by_slug("booking")["id"]
        self.admin.post("/api/admin/users", json={"username": "lead.booking", "display_name": "Booking lead",
                                                  "role": "reporter", "password": "longpassword"})
        self.blead = db.get_user_by_username("lead.booking")["id"]
        db.remove_membership(db.default_project_id(), self.blead)       # only in Booking
        db.set_membership(self.booking, self.blead, "triage_lead")
        db.set_membership(self.booking, db.get_user_by_username("dev.platform")["id"], "developer", ["bookings"])

    def login(self, username, password="longpassword"):
        client = self.app.test_client()
        self.assertEqual(client.post("/api/login", json={"username": username, "password": password}).status_code, 200)
        return client

    def booking_issue(self, client, title="Double booking", text="Two guests got the same room for the same night."):
        return self.submit_in(client, "booking", title, text)

    def submit_in(self, client, slug, title, text):
        from unittest import mock
        from triage import llm
        with mock.patch.object(llm, "call_model", return_value=llm_reply(module="api")):
            return client.post("/api/issues", json={"title": title, "description": text, "project": slug})

    # PA1: a lead of one project cannot see, list or triage another project's issues
    def test_pa1_leads_are_confined_to_their_project(self):
        blead = self.login("lead.booking")
        issue = self.booking_issue(blead).get_json()
        lead = self.as_user("lead")                                   # lead of the first project only
        self.assertEqual(lead.get(f"/api/issues/{issue['id']}").status_code, 404)
        self.assertEqual(lead.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept"}).status_code, 404)
        self.assertEqual(lead.get("/api/issues?scope=all&project=booking").status_code, 403)
        queue = blead.get("/api/issues?scope=all&project=booking").get_json()
        self.assertEqual([i["id"] for i in queue], [issue["id"]])
        self.assertEqual(blead.get("/api/issues?scope=all&project=simulated").status_code, 403)

    # PA2: one person, two roles: developer in Booking, lead nowhere else
    def test_pa2_roles_differ_by_project(self):
        blead = self.login("lead.booking")
        issue = self.booking_issue(blead).get_json()
        dev = db.get_user_by_username("dev.platform")["id"]
        res = blead.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept", "assignee_id": dev})
        self.assertEqual(res.get_json()["status"], "assigned")
        platform = self.as_user("dev.platform")
        assigned = platform.get("/api/issues?scope=assigned").get_json()
        self.assertEqual([(i["id"]) for i in assigned], [issue["id"]])
        self.assertEqual(platform.get(f"/api/issues/{issue['id']}").get_json()["project"]["slug"], "booking")
        self.assertEqual(platform.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept"}).status_code, 403)
        devs = blead.get("/api/developers?project=booking").get_json()
        self.assertEqual([d["id"] for d in devs], [dev])

    # PA3: removing someone from a project takes effect on their very next request
    def test_pa3_removal_revokes_access_at_once(self):
        blead = self.login("lead.booking")
        issue = self.booking_issue(blead).get_json()
        db.set_membership(self.booking, db.get_user_by_username("lead")["id"], "triage_lead")  # a second lead
        self.assertEqual(blead.get(f"/api/issues/{issue['id']}").status_code, 200)
        res = self.admin.delete(f"/api/admin/projects/booking/members/{self.blead}")
        self.assertEqual(res.status_code, 200)
        same_session = blead.get(f"/api/issues/{issue['id']}")
        self.assertEqual(same_session.status_code, 200)                # still the reporter of it...
        self.assertNotIn("plan", same_session.get_json())              # ...but read-only, no lead view
        self.assertEqual(blead.get("/api/issues?scope=all&project=booking").status_code, 403)

    # PA4: reporters report only into their own projects
    def test_pa4_reporting_needs_membership(self):
        acme = self.as_user("client.acme")                            # member of the first project only
        self.assertEqual(self.submit_in(acme, "booking", "Room error", "Booking page shows an error.").status_code, 400)
        res = self.submit_in(acme, "simulated", "Checkout error", "Checkout fails with an error today.")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["project"]["slug"], "simulated")
        me = acme.get("/api/me").get_json()
        self.assertEqual([p["slug"] for p in me["projects"]], ["simulated"])

    # PA5: insights are per project for leads; the administrator can see one project or all
    def test_pa5_metrics_per_project(self):
        blead = self.login("lead.booking")
        self.booking_issue(blead)
        self.submit(self.as_user("lead"), "Checkout error", "Checkout fails with an error for everyone.")
        self.assertEqual(blead.get("/api/metrics?project=booking").get_json()["total"], 1)
        self.assertEqual(self.admin.get("/api/metrics").get_json()["total"], 2)
        self.assertEqual(self.admin.get("/api/metrics?project=booking").get_json()["total"], 1)


class ProjectAITests(Base):
    """Phase 5: knowledge, classifier and modules are per project."""

    def setUp(self):
        super().setUp()
        admin = self.as_user("admin")
        admin.post("/api/admin/projects", json={"slug": "booking", "name": "Booking API", "tracker": "none",
                                                "modules": ["bookings", "payments", "other"]})
        self.booking = db.get_project_by_slug("booking")["id"]
        self.lead_id = db.get_user_by_username("lead")["id"]
        db.set_membership(self.booking, self.lead_id, "triage_lead")
        db.set_membership(self.booking, db.get_user_by_username("dev.platform")["id"], "developer", ["bookings"])
        self.lead = self.as_user("lead")

    def submit_in(self, slug, title, text, reply=None, side_effect=None):
        from unittest import mock
        from triage import llm
        kwargs = {"side_effect": side_effect} if side_effect else {"return_value": reply or llm_reply()}
        with mock.patch.object(llm, "call_model", **kwargs):
            return self.lead.post("/api/issues", json={"title": title, "description": text, "project": slug}).get_json()

    # PI1: a fix recorded in one project is never suggested in another
    def test_pi1_knowledge_is_isolated(self):
        text = "Gift card balance is not deducted when a discount code is also applied at checkout."
        first = self.submit_in("simulated", "Gift card ignored with discount", text)
        self.lead.post(f"/api/issues/{first['id']}/triage", json={"action": "accept"})
        self.assertEqual(db.get_issue(first["id"])["assignee_id"], db.get_user_by_username("dev.payments")["id"])
        dev = self.as_user("dev.payments")
        dev.post(f"/api/issues/{first['id']}/resolve", json={"resolution": "Gift card now applied before discounts."})
        again_a = self.submit_in("simulated", "Gift card not used with promo code", text)
        again_b = self.submit_in("booking", "Gift card not used with promo code", text)
        self.assertIn(first["id"], [s["id"] for s in again_a["similar_issues"]])
        self.assertNotIn(first["id"], [s["id"] for s in again_b["similar_issues"]])
        self.assertEqual(again_b["similar_issues"], [])                 # the seed fixes are not shared

    # PI2: a module that exists only in one project is accepted there and rejected elsewhere
    def test_pi2_modules_are_per_project(self):
        in_booking = self.submit_in("booking", "Double booking", "Two guests got the same room.",
                                    llm_reply(module="bookings"))
        self.assertEqual((in_booking["suggestion"]["module"], in_booking["suggestion"]["source"]), ("bookings", "llm"))
        in_first = self.submit_in("simulated", "Double booking", "Two guests got the same room.",
                                  llm_reply(module="bookings"))
        self.assertEqual(in_first["suggestion"]["source"], "fallback")  # invalid twice -> backup classifier

    # PI3: the model is told the project's modules
    def test_pi3_prompt_lists_project_modules(self):
        from triage import llm
        seen = {}

        def fake(msg, modules=None):
            seen["modules"] = modules
            return llm_reply(module="bookings")
        self.submit_in("booking", "Double booking", "Two guests got the same room.", side_effect=fake)
        self.assertEqual(seen["modules"], ["bookings", "payments", "other"])
        self.assertIn("bookings", llm.system_prompt(seen["modules"]))
        self.assertIn("bookings", str(llm.output_schema(seen["modules"])))

    # PI4: the backup classifier only answers with the project's modules
    def test_pi4_backup_classifier_uses_project_modules(self):
        os.environ["LLM_ENABLED"] = "false"
        try:
            issue = self.submit_in("booking", "Room page slow", "The room list page takes thirty seconds to load.")
        finally:
            os.environ["LLM_ENABLED"] = "true"
        self.assertEqual(issue["suggestion"]["source"], "fallback")
        self.assertIn(issue["suggestion"]["module"], ["bookings", "payments", "other"])

    # PI5: a project can opt in to the generic seeded fixes
    def test_pi5_optional_shared_seed(self):
        text = "Applying any discount code at checkout shows a server error and the order fails."
        self.assertEqual(self.submit_in("booking", "Discount code error", text)["similar_issues"], [])
        self.as_user("admin").patch("/api/admin/projects/booking", json={"share_seed": True})
        shared = self.submit_in("booking", "Discount code error", text)
        self.assertTrue(shared["similar_issues"])


class ProjectLogTests(Base):
    # PL8: the activity log can be read for one project
    def test_pl8_audit_filtered_by_project(self):
        admin = self.as_user("admin")
        admin.post("/api/admin/projects", json={"slug": "booking", "name": "Booking API", "tracker": "none"})
        self.submit(self.as_user("lead"), "Checkout error", "Checkout fails with an error for everyone.")
        booking = [e["action"] for e in admin.get("/api/admin/audit?project=booking").get_json()]
        first = [e["action"] for e in admin.get("/api/admin/audit?project=simulated").get_json()]
        self.assertEqual(booking, ["project_created"])
        self.assertIn("issue_submitted", first)
        self.assertNotIn("project_created", first)
        self.assertEqual(admin.get("/api/admin/audit?project=nope").status_code, 404)


class TeamOverviewTests(Base):
    # TO1: per-project roles and assignment counts, without issue content, for the admin only
    def test_to1_team_overview(self):
        admin = self.as_user("admin")
        admin.post("/api/admin/projects", json={"slug": "booking", "name": "Booking API", "tracker": "none"})
        lead = self.as_user("lead")
        issue = self.submit(lead, "Refund missing", "A refund did not appear in the customer account.").get_json()
        dev = db.get_user_by_username("dev.payments")["id"]
        lead.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept", "assignee_id": dev})
        data = admin.get("/api/admin/overview").get_json()
        first, booking = data
        self.assertEqual(first["roles"], {"triage_lead": 1, "developer": 3, "reporter": 2})
        payments = next(m for m in first["members"] if m["username"] == "dev.payments")
        self.assertEqual((payments["open"], payments["resolved"]), (1, 0))
        self.assertEqual(first["assigned_open"], 1)
        self.assertEqual(booking["roles"], {"triage_lead": 0, "developer": 0, "reporter": 0})
        self.assertNotIn("Refund missing", str(data))                   # counts only
        self.assertEqual(len(admin.get("/api/admin/overview?project=booking").get_json()), 1)
        self.assertEqual(lead.get("/api/admin/overview").status_code, 403)

    # TO2: removing someone from the first project survives a restart (the migration runs again)
    def test_to2_removal_survives_restart(self):
        admin = self.as_user("admin")
        tester = db.get_user_by_username("tester")["id"]
        admin.delete(f"/api/admin/projects/simulated/members/{tester}")
        create_app()                                                  # start-up runs the migration
        self.assertNotIn(db.default_project_id(), db.memberships(tester))


class CrossProjectFixTests(Base):
    """Regression tests for docs/multi_project_review.md (problems between two projects)."""

    def setUp(self):
        super().setUp()
        self.admin = self.as_user("admin")
        self.lead = self.as_user("lead")
        os.environ["SIM_TRACKER_DIR"] = self.tmp.name
        self.admin.post("/api/admin/projects", json={"slug": "second", "name": "Second", "tracker": "simulated",
                                                     "modules": ["bookings", "payments", "other"]})
        self.second = db.get_project_by_slug("second")["id"]
        self.first = db.default_project_id()
        db.set_membership(self.second, self.uid("lead"), "triage_lead")
        db.set_membership(self.second, self.uid("dev.payments"), "developer", ["payments"])

    def tearDown(self):
        os.environ.pop("SIM_TRACKER_DIR", None)
        for k in ("GH_A", "GH_SA", "GH_B", "GH_SB"):
            os.environ.pop(k, None)
        os.environ["TRACKER"] = "simulated"
        os.environ.pop("GITHUB_REPO", None)
        super().tearDown()

    def uid(self, username):
        return db.get_user_by_username(username)["id"]

    def sync(self, slug):
        from unittest import mock
        from triage import llm
        with mock.patch.object(llm, "call_model", return_value=llm_reply(module="payments")):
            return self.lead.post(f"/api/integration/sync?project={slug}")

    # RV1: with a second project, the first stops following .env and keeps its repository
    def test_rv1_first_project_stops_following_env(self):
        os.environ.update(TRACKER="github", GITHUB_REPO="acme/other")
        create_app()
        self.assertEqual(db.get_project(self.first)["tracker"], "simulated")
        self.assertEqual(db.get_project(self.first)["managed_by_env"], 0)

    # RV1b: a project with imported issues cannot be pointed at another repository
    def test_rv1b_no_repointing_after_import(self):
        self.sync("second")
        res = self.admin.patch("/api/admin/projects/second", json={"tracker": "github", "repo": "acme/x"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("mix two repositories", res.get_json()["error"])

    # RV2: an archived project neither imports nor writes back, and its issues are read-only
    def test_rv2_archived_project_is_read_only(self):
        self.sync("second")
        issue_id = db.find_external("simulated", "SIM-2", project_id=self.second)
        self.admin.patch("/api/admin/projects/second", json={"active": False})
        self.assertEqual(self.sync("second").status_code, 400)
        res = self.lead.post(f"/api/issues/{issue_id}/triage",
                             json={"action": "accept", "assignee_id": self.uid("dev.payments")})
        self.assertEqual(res.status_code, 400)
        self.assertIn("archived", res.get_json()["error"])
        pipeline = self.app.config["pipelines"].get(self.second)
        self.assertIsNone(pipeline.queue(issue_id, "comment", text="hello"))
        self.assertEqual(self.lead.get("/api/issues?scope=all&project=second").status_code, 200)  # still readable

    # RV3: reporting without a project never lands in an archived one
    def test_rv3_reporting_skips_archived_projects(self):
        acme = self.as_user("client.acme")
        db.remove_membership(self.first, self.uid("client.acme"))
        db.set_membership(self.second, self.uid("client.acme"), "reporter")
        self.admin.patch("/api/admin/projects/second", json={"active": False})
        res = self.submit(acme, "Checkout error", "Checkout fails today for everyone.")
        self.assertEqual(res.status_code, 400)

    # RV4: editing a user on the Users screen does not re-add them to the first project
    def test_rv4_users_screen_edit_keeps_removal(self):
        tester = self.uid("tester")
        self.admin.delete(f"/api/admin/projects/simulated/members/{tester}")
        self.admin.patch(f"/api/admin/users/{tester}", json={"display_name": "Renamed tester", "role": "reporter"})
        self.assertNotIn(self.first, db.memberships(tester))
        # becoming an administrator ends every membership
        dev = self.uid("dev.payments")
        self.admin.patch(f"/api/admin/users/{dev}", json={"role": "admin"})
        self.assertEqual(db.memberships(dev), {})

    # RV5: the old webhook URL keeps working after a second GitHub project is added
    def test_rv5_old_webhook_url_with_two_github_projects(self):
        import hashlib, hmac, json
        os.environ.update(GH_A="a", GH_SA="sa", GH_B="b", GH_SB="sb")
        r = self.admin.post("/api/admin/projects", json={"slug": "repo-a", "name": "Repo A", "tracker": "github",
                                                     "repo": "acme/a", "token_env": "GH_A", "secret_env": "GH_SA"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.admin.post("/api/admin/projects", json={"slug": "repo-b", "name": "Repo B", "tracker": "github",
                                                     "repo": "acme/b", "token_env": "GH_B", "secret_env": "GH_SB"})
        payload = json.dumps({"action": "edited", "issue": {"number": 5}}).encode()
        sig = "sha256=" + hmac.new(b"sa", payload, hashlib.sha256).hexdigest()
        res = self.app.test_client().post("/api/webhooks/github", data=payload,
                                          headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig})
        self.assertEqual(res.status_code, 200)                         # routed to the oldest: repo-a

    # RV6: open work in another project counts against a developer
    def test_rv6_workload_counts_all_projects(self):
        from triage import assignment
        for n in range(3):
            i = db.insert_issue(f"t{n}", "x", None, {"ai_type": "bug", "ai_module": "payments",
                                                     "ai_priority": "low", "ai_confidence": 0.5},
                                {"source": "desk", "project_id": self.first})
            db.save_triage_decision(i, "accept", "bug", "payments", "low", self.uid("dev.payments"), None)
        top = assignment.rank_developers("payments", project_id=self.second)[0]
        self.assertEqual(top["open_count"], 3)
        self.assertEqual(top["score"], 3.0 - 3)
        self.assertTrue(any("3 in other projects" in r for r in top["reasons"]))

    # RV7: issues sent back to the queue get a fresh plan without the removed developer
    def test_rv7_released_issues_are_replanned(self):
        self.sync("second")
        issue_id = db.find_external("simulated", "SIM-1", project_id=self.second)
        self.lead.post(f"/api/issues/{issue_id}/triage",
                       json={"action": "accept", "assignee_id": self.uid("dev.payments")})
        self.admin.delete(f"/api/admin/projects/second/members/{self.uid('dev.payments')}?release=true")
        plan = db.latest_plan(issue_id)
        self.assertEqual((db.get_issue(issue_id)["status"], plan["status"]), ("new", "proposed"))
        self.assertNotEqual(plan["plan"]["assignee_id"], self.uid("dev.payments"))

    # RV9: lists across projects carry the project name
    def test_rv9_lists_name_the_project(self):
        self.submit(self.as_user("client.acme"), "Checkout error", "Checkout fails today for everyone.")
        mine = self.as_user("client.acme").get("/api/issues?scope=mine").get_json()
        self.assertEqual(mine[0]["project_name"], "Simulated tracker")
