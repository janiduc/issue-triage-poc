"""Tests for the triage agent, the rule-based ranking, the tracker pipeline and the connectors.

Run with:  python -m unittest -v
All external calls (AI model, GitHub, Jira) are mocked; the simulated tracker is a temp file.
"""
import hashlib
import hmac
import json
import os
from unittest import mock

from app import create_app
from tests.test_triage import Base, llm_reply
from triage import agent, assignment, db, llm
from triage.integrations import http as tracker_http
from triage.integrations.base import SyncError, TriageWrite
from triage.integrations.github import GitHubConnector
from triage.integrations.jira import JiraConnector, adf_to_text


def tool_use(name, args, call_id="t1"):
    return {"content": [{"type": "tool_use", "id": call_id, "name": name, "input": args}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 900, "output_tokens": 80}}


def uid(username):
    return db.get_user_by_username(username)["id"]


class AgentTests(Base):
    def agent_script(self, *responses):
        self.agent_patch.stop()
        return mock.patch.object(agent, "call_agent_model", side_effect=list(responses))

    # AG1: the agent investigates with tools and proposes a validated plan
    def test_ag1_agent_plan_happy_path(self):
        dev = uid("dev.payments")
        script = self.agent_script(
            tool_use("search_similar_issues", {"query": "discount code checkout 500 error"}),
            tool_use("rank_developers", {"module": "payments", "similar_issue_ids": [2]}),
            tool_use("submit_plan", {
                "type": "bug", "module": "payments", "priority": "critical", "action": "assign",
                "assignee_id": dev, "assignee_reason": "Owns payments and fixed #2.",
                "comment_for_reporter": "Thanks, the team is looking into this.",
                "extra_labels": ["regression", "made-up-label"], "duplicate_of": 2,
                "confidence": 0.9, "summary": "Likely the coupon expiry bug again."}))
        lead = self.as_user("lead")
        with script:
            issue = self.submit(lead, "Discount code gives server error at checkout",
                                "Applying a discount code at checkout shows a 500 error.").get_json()
        plan = lead.get(f"/api/issues/{issue['id']}").get_json()["plan"]
        self.assertEqual(plan["source"], "agent")
        self.assertEqual([t["tool"] for t in plan["trace"]],
                         ["search_similar_issues", "rank_developers", "submit_plan"])
        self.assertEqual(plan["plan"]["assignee_id"], dev)
        self.assertIn("regression", plan["plan"]["labels"])
        self.assertNotIn("made-up-label", plan["plan"]["labels"])  # only allowed extras
        self.assertEqual(plan["plan"]["duplicate_of"], 2)
        self.assertEqual(plan["steps"], 3)

    # AG2: the agent cannot assign outside the rule-based shortlist or cite unseen issues
    def test_ag2_agent_output_is_constrained(self):
        script = self.agent_script(
            tool_use("submit_plan", {
                "type": "bug", "module": "payments", "priority": "high", "action": "assign",
                "assignee_id": uid("client.acme"),  # a reporter, not a shortlisted developer
                "comment_for_reporter": "Thanks.", "duplicate_of": 7,  # never returned by a tool
                "confidence": 0.8, "summary": "x"}))
        lead = self.as_user("lead")
        with script:
            issue = self.submit(lead, "Refunds missing", "Refunds from yesterday are not in accounts.").get_json()
        plan = lead.get(f"/api/issues/{issue['id']}").get_json()["plan"]["plan"]
        self.assertEqual(plan["assignee_id"], uid("dev.payments"))  # replaced by top candidate
        self.assertIsNone(plan["duplicate_of"])
        self.assertEqual(len(plan["notes"]), 2)

    # AG3 (fallback): an agent that never finishes is stopped and the rules planner takes over
    def test_ag3_step_limit_falls_back_to_rules(self):
        script = self.agent_script(*[tool_use("search_similar_issues", {"query": "x"})] * agent.MAX_STEPS)
        lead = self.as_user("lead")
        with script:
            issue = self.submit(lead, "Slow search", "Search takes ten seconds for big stores.").get_json()
        plan = lead.get(f"/api/issues/{issue['id']}").get_json()["plan"]
        self.assertEqual(plan["source"], "rules")
        self.assertIn("did not finish", plan["fallback_reason"])

    # AG4: a vague issue gets a rules plan that asks the reporter a question
    def test_ag4_rules_plan_requests_info_for_vague_issue(self):
        lead = self.as_user("lead")
        issue = self.submit(lead, "Error", "It broke", llm_reply(confidence=0.3)).get_json()
        plan = lead.get(f"/api/issues/{issue['id']}").get_json()["plan"]["plan"]
        self.assertEqual(plan["action"], "request_info")
        self.assertTrue(plan["question_for_reporter"])

    # AG5: approvals and edits are recorded, giving an agent-agreement metric
    def test_ag5_plan_decisions_recorded(self):
        lead = self.as_user("lead")
        a = self.submit(lead, "Checkout error", "Checkout fails with an error for all customers.").get_json()
        b = self.submit(lead, "Refund missing", "A refund did not appear in the customer account.").get_json()
        lead.post(f"/api/issues/{a['id']}/triage", json={"action": "accept"})
        lead.post(f"/api/issues/{b['id']}/triage",
                  json={"action": "accept", "assignee_id": uid("dev.platform")})
        m = lead.get("/api/metrics").get_json()["agent"]
        self.assertEqual(m["decided"], 2)
        self.assertEqual(m["approved_unchanged"], 1)
        self.assertEqual(m["assignee_kept"], 0.5)


class RankingTests(Base):
    # RK1: ownership and similar-fix history rank first; each open assignment lowers the score
    def test_rk1_rule_based_ranking(self):
        ranked = assignment.rank_developers("payments", [2, 15])
        self.assertEqual(ranked[0]["name"], "Developer, payments and reporting")
        self.assertTrue(any("owns the payments module" in r for r in ranked[0]["reasons"]))
        self.assertTrue(any("fixed similar issue" in r for r in ranked[0]["reasons"]))
        before = ranked[0]["score"]
        lead = self.as_user("lead")
        issue = self.submit(lead, "Payment bug", "Card payments fail for some customers today.").get_json()
        lead.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept"})
        after = assignment.rank_developers("payments", [2, 15])[0]["score"]
        self.assertEqual(after, before - 1.0)


class SimulatedPipelineTests(Base):
    def tracker(self):
        return self.app.config["pipeline"].connector.state()

    def sim(self, key):
        return next(i for i in self.tracker()["issues"] if i["key"] == key)

    def desk_issue(self, lead, key):
        issues = lead.get("/api/issues?status=").get_json()
        return next(i for i in issues if i["external_key"] == key)

    def sync(self, client):
        with mock.patch.object(llm, "call_model", return_value=llm_reply()):
            return client.post("/api/integration/sync").get_json()

    # PL1: import is idempotent and secrets are redacted on the way in
    def test_pl1_import_and_dedupe(self):
        lead = self.as_user("lead")
        self.assertEqual(self.sync(lead)["imported"], 4)
        self.assertEqual(self.sync(lead)["imported"], 0)
        sim1 = self.desk_issue(lead, "SIM-1")
        self.assertNotIn("4111 1111 1111 1111", sim1["description"])
        self.assertEqual(sim1["reporter_name"], "store-owner-bluefin")

    # PL2: approval writes labels, priority, assignee and comment back to the tracker
    def test_pl2_approval_writes_back(self):
        lead = self.as_user("lead")
        self.sync(lead)
        issue = self.desk_issue(lead, "SIM-2")
        res = lead.post(f"/api/issues/{issue['id']}/triage", json={
            "action": "override", "module": "reporting", "priority": "medium",
            "assignee_id": uid("dev.payments"), "comment": "Thanks, our reporting team is on it."}).get_json()
        self.assertEqual(res["sync_status"], "ok")
        t = self.sim("SIM-2")
        self.assertIn("priority-medium", t["labels"])
        self.assertIn("module-reporting", t["labels"])
        self.assertEqual(t["assignee"], "dev.payments")
        self.assertIn("reporting team is on it", t["comments"][-1]["body"])

    # PL3: question to reporter -> reply in tracker -> sync re-analyses the issue
    def test_pl3_request_info_round_trip(self):
        lead = self.as_user("lead")
        self.sync(lead)
        issue = self.desk_issue(lead, "SIM-3")
        lead.post(f"/api/issues/{issue['id']}/request-info", json={"message": "Which page and what error?"})
        self.assertIn("Which page", self.sim("SIM-3")["comments"][-1]["body"])
        lead.post("/api/integration/simulated/issues/SIM-3/reply",
                  json={"author": "new-client-oak", "body": "Checkout page, error 500 when paying."})
        summary = self.sync(lead)
        self.assertEqual(summary["replies"], 1)
        after = lead.get(f"/api/issues/{issue['id']}").get_json()
        self.assertEqual(after["status"], "new")
        self.assertIn("error 500", after["description"])

    # PL4: resolving in the desk closes the tracker issue with the resolution
    def test_pl4_resolution_closes_tracker_issue(self):
        lead, dev = self.as_user("lead"), self.as_user("dev.payments")
        self.sync(lead)
        issue = self.desk_issue(lead, "SIM-2")
        lead.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept", "assignee_id": uid("dev.payments")})
        dev.post(f"/api/issues/{issue['id']}/resolve", json={"resolution": "Excluded refunded orders from export totals."})
        t = self.sim("SIM-2")
        self.assertEqual(t["state"], "closed")
        self.assertIn("Excluded refunded orders", t["comments"][-1]["body"])

    # PL5: an issue closed directly in the tracker is resolved in the desk and joins the knowledge base
    def test_pl5_closed_in_tracker_feeds_knowledge_base(self):
        lead = self.as_user("lead")
        self.sync(lead)
        issue = self.desk_issue(lead, "SIM-4")
        lead.post("/api/integration/simulated/issues/SIM-4/close",
                  json={"author": "dev", "body": "Added a wishlist page with save-for-later buttons."})
        self.assertEqual(self.sync(lead)["closed"], 1)
        self.assertEqual(lead.get(f"/api/issues/{issue['id']}").get_json()["status"], "resolved")
        self.assertIn(issue["id"], [k["id"] for k in db.knowledge_base()])

    # PL6 (fallback): tracker outage -> write-back queued, shown as failed, retried successfully
    def test_pl6_outbox_retry(self):
        lead = self.as_user("lead")
        self.sync(lead)
        issue = self.desk_issue(lead, "SIM-1")
        connector = self.app.config["pipeline"].connector
        with mock.patch.object(connector, "apply_triage", side_effect=SyncError("Tracker unreachable")):
            res = lead.post(f"/api/issues/{issue['id']}/triage", json={"action": "accept"}).get_json()
        self.assertEqual(res["sync_status"], "failed")
        self.assertEqual(res["status"], "assigned")  # the desk decision is not lost
        res = lead.post(f"/api/issues/{issue['id']}/sync-retry").get_json()
        self.assertEqual(res["sync_status"], "ok")
        self.assertIn("triage-desk", self.sim("SIM-1")["labels"])


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data


class GitHubTests(Base):
    # GH1: reads skip pull requests; writes go to the right endpoints with our marker
    def test_gh1_github_read_and_write(self):
        gh = GitHubConnector("token", "acme/shop")
        items = [{"number": 5, "title": "Bug", "body": "b", "html_url": "u", "user": {"login": "x"}, "labels": []},
                 {"number": 6, "title": "PR", "pull_request": {}, "labels": []}]
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw.get("json")))
            return FakeResponse(200, items if method == "GET" else {})
        with mock.patch.object(tracker_http.requests, "request", side_effect=fake):
            found = gh.fetch_open_issues()
            gh.apply_triage("5", TriageWrite(labels=["priority-high"], priority="high",
                                             assignee="octodev", comment="On it."))
        self.assertEqual([i.key for i in found], ["#5"])
        methods = [(m, u.split("/issues")[1]) for m, u, _ in calls[1:]]
        self.assertEqual(methods, [("POST", "/5/labels"), ("POST", "/5/assignees"), ("POST", "/5/comments")])
        self.assertIn("[triage-desk]", calls[-1][2]["body"])

    # GH2: webhooks are accepted only with a valid signature, then imported and triaged
    def test_gh2_signed_webhook(self):
        os.environ.update(TRACKER="github", GITHUB_TOKEN="t", GITHUB_REPO="acme/shop",
                          GITHUB_WEBHOOK_SECRET="s3cret")
        try:
            client = create_app().test_client()
            payload = json.dumps({"action": "opened", "issue": {
                "number": 42, "title": "Login fails", "body": "Users cannot log in since the update.",
                "html_url": "https://github.com/acme/shop/issues/42", "user": {"login": "ann"}}}).encode()
            sig = "sha256=" + hmac.new(b"s3cret", payload, hashlib.sha256).hexdigest()
            headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
            bad = client.post("/api/webhooks/github", data=payload, headers=headers | {"X-Hub-Signature-256": "sha256=00"})
            self.assertEqual(bad.status_code, 401)
            with mock.patch.object(llm, "call_model", return_value=llm_reply(module="authentication")):
                ok = client.post("/api/webhooks/github", data=payload, headers=headers | {"X-Hub-Signature-256": sig})
            self.assertTrue(ok.get_json()["handled"])
            self.assertIsNotNone(db.find_external("github", "42"))
        finally:
            for k in ("GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_WEBHOOK_SECRET"):
                os.environ.pop(k, None)
            os.environ["TRACKER"] = "simulated"


class JiraTests(Base):
    def jira(self):
        return JiraConnector("https://acme.atlassian.net", "a@b.c", "tok", "SHOP")

    # JR1: ADF text conversion and pagination with nextPageToken
    def test_jr1_adf_and_pagination(self):
        adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Line one"}]},
                                          {"type": "paragraph", "content": [{"type": "text", "text": "Line two"}]}]}
        self.assertEqual(adf_to_text(adf).strip(), "Line one\nLine two")
        page = lambda key, token: FakeResponse(200, {"issues": [{"key": key, "fields": {
            "summary": "S", "description": adf, "reporter": {"displayName": "R"}}}], "nextPageToken": token})
        with mock.patch.object(tracker_http.requests, "request",
                               side_effect=[page("SHOP-1", "abc"), page("SHOP-2", None)]) as m:
            found = self.jira().fetch_open_issues()
        self.assertEqual([i.key for i in found], ["SHOP-1", "SHOP-2"])
        self.assertIn("/rest/api/3/search/jql", m.call_args_list[0].args[1])
        self.assertEqual(m.call_args_list[1].kwargs["params"]["nextPageToken"], "abc")

    # JR2: triage write-back maps priority and labels; closing uses a Done transition
    def test_jr2_write_back_and_close(self):
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url.split("/rest/api/3")[1], kw.get("json")))
            if url.endswith("/transitions") and method == "GET":
                return FakeResponse(200, {"transitions": [
                    {"id": "11", "to": {"statusCategory": {"key": "indeterminate"}}},
                    {"id": "31", "to": {"statusCategory": {"key": "done"}}}]})
            return FakeResponse(204, None)
        with mock.patch.object(tracker_http.requests, "request", side_effect=fake):
            self.jira().apply_triage("SHOP-7", TriageWrite(labels=["priority-critical"], priority="critical",
                                                           assignee="acc-123", comment="Thanks."))
            self.jira().close_issue("SHOP-7", "Fixed.")
        put = calls[0][2]
        self.assertEqual(put["fields"]["priority"]["name"], "Highest")
        self.assertEqual(put["update"]["labels"], [{"add": "priority-critical"}])
        self.assertEqual(calls[1], ("PUT", "/issue/SHOP-7/assignee", {"accountId": "acc-123"}))
        self.assertEqual(calls[-1], ("POST", "/issue/SHOP-7/transitions", {"transition": {"id": "31"}}))

    # JR3 (error): no Done transition available -> clear error, not a crash
    def test_jr3_missing_done_transition(self):
        responses = [FakeResponse(201, {}), FakeResponse(200, {"transitions": []})]
        with mock.patch.object(tracker_http.requests, "request", side_effect=responses):
            with self.assertRaises(SyncError):
                self.jira().close_issue("SHOP-7", "Fixed.")
