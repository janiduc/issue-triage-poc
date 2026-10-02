"""Flask web application: role-based API and the single-page front end.

Roles and what each can do:
  reporter     submit issues, track own issues, add information when asked
  triage_lead  review AI suggestions, accept/override, assign developers, request info, metrics
  developer    work on assigned issues (with AI fix suggestions), record resolutions, submit issues
  admin        manage users and roles, AI and agent settings, tracker integration, audit log,
               metrics (no access to issue content)

Pipeline: tracker (GitHub / Jira / simulated) -> import -> AI classification -> triage agent
proposes a plan -> triage lead edits and approves -> write-back to the tracker -> developer
resolves -> closing comment in the tracker -> fix added to the knowledge base.
"""
import json
import logging
import os
import re
import threading

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

from triage import config, db
from triage.auth import authenticate, current_user, require_roles, reset_failed_logins
from triage.agent import EXTRA_LABELS
from triage.integrations import get_connector
from triage.integrations.base import SyncError
from triage.pipeline import Pipeline
from triage.service import TriageService, agent_enabled, ai_enabled, confidence_threshold, to_api
from triage.validation import ValidationError, validate_issue_input

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("triage")

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_field(data, name, min_len, label):
    value = str(data.get(name) or "").strip()
    if len(value) < min_len:
        raise ValidationError(f"{label} must be at least {min_len} characters.")
    if len(value) > 3000:
        raise ValidationError(f"{label} must be 3000 characters or fewer.")
    return value


def sort_queue(rows):
    rows.sort(key=lambda r: (config.PRIORITY_RANK.get(r["final_priority"] or r["ai_priority"], 9),
                             -r["id"]))
    return rows


def view_role_for(issue, user):
    """Which view of an issue this user may see: 'full', 'reporter', or None (no access)."""
    role = user["role"]
    if role == "triage_lead":
        return "full"
    if role == "developer" and issue["assignee_id"] == user["id"]:
        return "full"
    if role in ("reporter", "developer") and issue["reporter_id"] == user["id"]:
        return "reporter"
    return None


def create_app():
    app = Flask(__name__, static_folder="static")
    app.config.update(SECRET_KEY=config.secret_key(), SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax")
    db.init_db()
    service = TriageService()
    pipeline = Pipeline(service, get_connector())
    app.config.update(service=service, pipeline=pipeline)

    def load_issue_for(issue_id):
        issue = db.get_issue(issue_id)
        if not issue or issue["is_seed"]:
            return None, None
        return issue, view_role_for(issue, g.user)

    def issue_response(issue_id, status=200):
        issue = db.get_issue(issue_id)
        view = view_role_for(issue, g.user)
        data = to_api(issue, "reporter" if view == "reporter" else g.user["role"])
        if g.user["role"] == "triage_lead":
            data["plan"] = db.latest_plan(issue_id)
        if view == "full":
            data["tracker_label"] = pipeline.connector.label if issue["source"] == pipeline.name else None
        return jsonify(data), status

    def record_plan_decision(issue_id, final):
        """Compare what the lead approved with what the agent proposed (for evaluation)."""
        plan = db.latest_plan(issue_id)
        if not plan or plan["status"] != "proposed":
            return
        proposed = plan["plan"]
        changed = []
        for field in ("type", "module", "priority"):
            if final.get(field) and final[field] != proposed["classification"][field]:
                changed.append(field)
        for field in ("action", "assignee_id", "comment_for_reporter", "question_for_reporter"):
            if field in final and final[field] != proposed.get(field):
                changed.append(field)
        db.decide_plan(plan["id"], "edited" if changed else "approved",
                       {"changed": changed, "final": final}, g.user["id"])

    # ---------- errors ----------

    @app.errorhandler(ValidationError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Exception)
    def server_error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        log.exception("Unhandled error")
        return jsonify(error="Something went wrong on the server. Nothing was saved."), 500

    # ---------- pages and session ----------

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.post("/api/login")
    def login():
        data = body()
        user, error = authenticate(data.get("username"), data.get("password"))
        if not user:
            db.audit(None, "login_failed", detail={"username": str(data.get("username"))[:50]})
            return jsonify(error=error), 401
        session.clear()
        session["user_id"] = user["id"]
        db.audit(user["id"], "login")
        return jsonify(user)

    @app.post("/api/logout")
    def logout():
        user = current_user()
        if user:
            db.audit(user["id"], "logout")
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    @require_roles()
    def me():
        return jsonify(g.user)

    @app.get("/api/options")
    @require_roles()
    def options():
        return jsonify(types=config.ISSUE_TYPES, modules=config.MODULES,
                       priorities=config.PRIORITIES, roles=config.ROLES,
                       role_labels=config.ROLE_LABELS)

    @app.get("/api/health")
    @require_roles("triage_lead", "admin")
    def health():
        return jsonify(llm_enabled=ai_enabled(), agent_enabled=agent_enabled(),
                       api_key_configured=config.llm_enabled(),
                       model=config.llm_model(), similarity_method=service.index.method,
                       knowledge_base_size=len(service.index.rows))

    # ---------- issues ----------

    @app.post("/api/issues")
    @require_roles("reporter", "developer", "triage_lead")
    def create_issue():
        data = validate_issue_input(request.get_json(silent=True))
        issue_id = service.triage_new(data["title"], data["description"], g.user["id"])
        db.audit(g.user["id"], "issue_submitted", issue_id)
        return issue_response(issue_id, 201)

    @app.get("/api/issues")
    @require_roles("reporter", "developer", "triage_lead")
    def list_issues():
        role = g.user["role"]
        default_scope = {"reporter": "mine", "developer": "assigned", "triage_lead": "all"}[role]
        scope = request.args.get("scope", default_scope)
        allowed = {"reporter": {"mine"}, "developer": {"mine", "assigned"},
                   "triage_lead": {"mine", "all"}}[role]
        if scope not in allowed:
            return jsonify(error="Your role cannot view that list."), 403
        status = request.args.get("status") or None
        if status and status not in config.ISSUE_STATUSES:
            raise ValidationError("Unknown status.")
        rows = db.list_issues(
            status=status,
            reporter_id=g.user["id"] if scope == "mine" else None,
            assignee_id=g.user["id"] if scope == "assigned" else None)
        shape = "reporter" if scope == "mine" else role
        return jsonify([to_api(r, shape) for r in sort_queue(rows)])

    @app.get("/api/issues/<int:issue_id>")
    @require_roles("reporter", "developer", "triage_lead")
    def get_issue(issue_id):
        issue, view = load_issue_for(issue_id)
        if not issue or not view:
            return jsonify(error="Issue not found."), 404  # do not reveal others' issues exist
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/triage")
    @require_roles("triage_lead")
    def triage_decision(issue_id):
        """The lead approves (possibly after editing) the agent's plan: classify and assign."""
        issue, _ = load_issue_for(issue_id)
        if not issue:
            return jsonify(error="Issue not found."), 404
        if issue["status"] != "new":
            raise ValidationError("Only issues awaiting triage can be triaged.")
        data = body()
        action = data.get("action")
        if action not in ("accept", "override"):
            raise ValidationError("Action must be 'accept' or 'override'.")
        plan = db.latest_plan(issue_id)
        proposed = plan["plan"] if plan else None
        base = proposed["classification"] if proposed else {
            "type": issue["ai_type"], "module": issue["ai_module"], "priority": issue["ai_priority"]}
        final = dict(base)
        if action == "override":
            for field, allowed in (("type", config.ISSUE_TYPES), ("module", config.MODULES),
                                   ("priority", config.PRIORITIES)):
                if data.get(field):
                    if data[field] not in allowed:
                        raise ValidationError(f"Invalid {field}.")
                    final[field] = data[field]
        assignee_id = data.get("assignee_id") or (proposed or {}).get("assignee_id")
        dev = db.get_user(assignee_id) if assignee_id else None
        if not dev or dev["role"] != "developer" or not dev["active"]:
            raise ValidationError("Choose an active developer to assign.")
        comment = data.get("comment")
        if comment is None and proposed:
            comment = proposed["comment_for_reporter"]
        comment = (str(comment or "").strip()[:1500]) or None
        extras = [l for l in (data.get("extra_labels") if "extra_labels" in data
                              else [l for l in (proposed or {}).get("labels", []) if l in EXTRA_LABELS])
                  if l in EXTRA_LABELS]
        note = (str(data.get("note") or "").strip() or None)

        db.save_triage_decision(issue_id, action, final["type"], final["module"],
                                final["priority"], assignee_id, note, comment)
        record_plan_decision(issue_id, final | {"action": "assign", "assignee_id": assignee_id,
                                                "comment_for_reporter": comment})
        changed = [f for f in final if final[f] != issue[f"ai_{f}"]]
        db.audit(g.user["id"], f"triage_{action}", issue_id,
                 {"assignee_id": assignee_id, "changed": changed})
        if data.get("post_to_tracker", True):
            write = pipeline.triage_write(db.get_issue(issue_id), extras, comment)
            pipeline.queue(issue_id, "triage", **write)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/request-info")
    @require_roles("triage_lead")
    def request_info(issue_id):
        issue, _ = load_issue_for(issue_id)
        if not issue:
            return jsonify(error="Issue not found."), 404
        if issue["status"] != "new":
            raise ValidationError("You can only ask for information on issues awaiting triage.")
        message = text_field(body(), "message", 5, "Your question")
        db.request_info(issue_id, message)
        record_plan_decision(issue_id, {"action": "request_info", "question_for_reporter": message})
        db.audit(g.user["id"], "info_requested", issue_id)
        pipeline.queue(issue_id, "comment", text=message)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/replan")
    @require_roles("triage_lead")
    def replan(issue_id):
        issue, _ = load_issue_for(issue_id)
        if not issue or issue["status"] != "new":
            raise ValidationError("The agent can only re-plan issues awaiting triage.")
        service.create_plan(issue_id)
        db.audit(g.user["id"], "agent_replanned", issue_id)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/sync-retry")
    @require_roles("triage_lead", "developer")
    def sync_retry(issue_id):
        issue, view = load_issue_for(issue_id)
        if not issue or view != "full":
            return jsonify(error="Issue not found."), 404
        if not issue["sync_pending"]:
            raise ValidationError("Nothing is waiting to be sent for this issue.")
        pipeline.deliver(issue_id)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/add-info")
    @require_roles("reporter", "developer", "triage_lead")
    def add_info(issue_id):
        issue, _ = load_issue_for(issue_id)
        if not issue or issue["reporter_id"] != g.user["id"]:
            return jsonify(error="Issue not found."), 404
        if issue["status"] != "needs_info":
            raise ValidationError("No information has been requested for this issue.")
        details = text_field(body(), "details", 5, "Additional information")
        service.retriage_with_info(issue, details)
        db.audit(g.user["id"], "info_added", issue_id)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/resolve")
    @require_roles("developer", "triage_lead")
    def resolve(issue_id):
        issue, view = load_issue_for(issue_id)
        if not issue or view != "full":
            return jsonify(error="Issue not found."), 404
        if issue["status"] != "assigned":
            raise ValidationError("Only assigned issues can be resolved.")
        resolution = text_field(body(), "resolution", 5, "Resolution")
        db.save_resolution(issue_id, resolution, g.user["id"])
        db.audit(g.user["id"], "issue_resolved", issue_id)
        service.refresh()  # the fix now helps with future similar issues
        if body().get("close_in_tracker", True):
            pipeline.queue(issue_id, "close", text=f"Resolved: {resolution}")
        return issue_response(issue_id)

    @app.get("/api/developers")
    @require_roles("triage_lead")
    def developers():
        return jsonify([{"id": d["id"], "name": d["display_name"], "modules": d["modules"],
                         "open_count": d["open_count"]} for d in db.developer_workload()])

    # ---------- insights ----------

    @app.get("/api/metrics")
    @require_roles("triage_lead", "admin")
    def metrics():
        rows = db.list_issues()
        reviewed = [r for r in rows if r["review_action"]]
        llm_rows = [r for r in rows if r["ai_source"] == "llm"]

        def agreement(field):
            if not reviewed:
                return None
            return round(sum(r[f"ai_{field}"] == r[f"final_{field}"] for r in reviewed)
                         / len(reviewed), 2)

        def avg(values):
            values = [v for v in values if v is not None]
            return round(sum(values) / len(values), 1) if values else None

        return jsonify(
            total=len(rows),
            by_status={s: sum(r["status"] == s for r in rows) for s in config.ISSUE_STATUSES},
            reviewed=len(reviewed),
            accepted=sum(r["review_action"] == "accept" for r in reviewed),
            overridden=sum(r["review_action"] == "override" for r in reviewed),
            fallback_rate=round(sum(r["ai_source"] == "fallback" for r in rows) / len(rows), 2)
            if rows else None,
            agreement={f: agreement(f) for f in ("type", "module", "priority")},
            avg_latency_ms=avg([r["latency_ms"] for r in rows]),
            avg_tokens_in=avg([r["tokens_in"] for r in llm_rows]),
            avg_tokens_out=avg([r["tokens_out"] for r in llm_rows]),
            agent=agent_metrics(),
            sync_failures=len(db.failed_syncs()),
        )

    def agent_metrics():
        plans = db.plan_rows()
        decided = [p for p in plans if p["status"] in ("approved", "edited")]
        agent_plans = [p for p in plans if p["source"] == "agent"]

        def changed(p, field):
            return field in json.loads(p["decision_json"] or "{}").get("changed", [])

        return {
            "plans": len(plans), "by_agent": len(agent_plans),
            "by_rules": sum(p["source"] == "rules" for p in plans),
            "decided": len(decided),
            "approved_unchanged": sum(p["status"] == "approved" for p in decided),
            "assignee_kept": (round(sum(not changed(p, "assignee_id") for p in decided) / len(decided), 2)
                              if decided else None),
            "comment_kept": (round(sum(not changed(p, "comment_for_reporter") for p in decided) / len(decided), 2)
                             if decided else None),
            "avg_steps": avg_of([p["steps"] for p in agent_plans]),
            "avg_tokens_in": avg_of([p["tokens_in"] for p in agent_plans]),
            "avg_tokens_out": avg_of([p["tokens_out"] for p in agent_plans]),
        }

    def avg_of(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 1) if values else None

    # ---------- administration ----------

    @app.get("/api/admin/users")
    @require_roles("admin")
    def admin_users():
        return jsonify(db.list_users())

    def clean_modules(data, role):
        modules = data.get("modules") or []
        if role != "developer":
            return []
        if not isinstance(modules, list) or any(m not in config.MODULES for m in modules):
            raise ValidationError("Unknown module in the list.")
        return modules

    @app.post("/api/admin/users")
    @require_roles("admin")
    def admin_create_user():
        data = body()
        username = str(data.get("username") or "").strip().lower()
        if not USERNAME_RE.match(username):
            raise ValidationError("Username: 3–30 lowercase letters, numbers, dots, dashes or underscores.")
        if db.get_user_by_username(username):
            raise ValidationError("That username is already taken.")
        role = data.get("role")
        if role not in config.ROLES:
            raise ValidationError("Choose a valid role.")
        name = text_field(data, "display_name", 2, "Display name")
        password = str(data.get("password") or "")
        if len(password) < 8:
            raise ValidationError("Password must be at least 8 characters.")
        user_id = db.create_user(username, name, role, password, clean_modules(data, role))
        db.audit(g.user["id"], "user_created", detail={"user_id": user_id, "role": role})
        return jsonify(db.user_to_dict(db.get_user(user_id))), 201

    @app.patch("/api/admin/users/<int:user_id>")
    @require_roles("admin")
    def admin_update_user(user_id):
        target = db.get_user(user_id)
        if not target:
            return jsonify(error="User not found."), 404
        data = body()
        role = data.get("role", target["role"])
        if role not in config.ROLES:
            raise ValidationError("Choose a valid role.")
        if user_id == g.user["id"] and (role != "admin" or data.get("active") is False):
            raise ValidationError("You cannot remove your own admin access or deactivate yourself.")
        password = data.get("password")
        if password is not None and len(str(password)) < 8:
            raise ValidationError("Password must be at least 8 characters.")
        active = data.get("active")
        db.update_user(user_id, role=role,
                       display_name=(str(data["display_name"]).strip() or None)
                       if "display_name" in data else None,
                       active=None if active is None else int(bool(active)),
                       modules=clean_modules(data, role) if ("modules" in data or role != "developer") else None,
                       password=password,
                       github_login=(str(data["github_login"]).strip() or None) if "github_login" in data else None,
                       jira_account_id=(str(data["jira_account_id"]).strip() or None)
                       if "jira_account_id" in data else None)
        if password:
            reset_failed_logins(target["username"])
        changed = sorted(k if k != "password" else "password_reset" for k in data)
        db.audit(g.user["id"], "user_updated", detail={"user_id": user_id, "changed": changed})
        return jsonify(db.user_to_dict(db.get_user(user_id)))

    @app.get("/api/admin/settings")
    @require_roles("admin")
    def admin_settings():
        return jsonify(llm_enabled=db.get_setting("llm_enabled", "true") == "true",
                       agent_enabled=db.get_setting("agent_enabled", "true") == "true",
                       api_key_configured=config.llm_enabled(), model=config.llm_model(),
                       confidence_threshold=confidence_threshold())

    @app.put("/api/admin/settings")
    @require_roles("admin")
    def admin_update_settings():
        data = body()
        for key in ("llm_enabled", "agent_enabled"):
            if key in data:
                db.set_setting(key, "true" if data[key] else "false")
        if "confidence_threshold" in data:
            try:
                value = float(data["confidence_threshold"])
            except (TypeError, ValueError):
                raise ValidationError("Confidence threshold must be a number.")
            if not 0 <= value <= 1:
                raise ValidationError("Confidence threshold must be between 0 and 1.")
            db.set_setting("confidence_threshold", value)
        db.audit(g.user["id"], "settings_changed", detail=data)
        return admin_settings()

    @app.get("/api/admin/audit")
    @require_roles("admin")
    def admin_audit():
        limit = min(int(request.args.get("limit", 100)), 500)
        return jsonify(db.audit_entries(limit))

    # ---------- tracker integration ----------

    @app.get("/api/integration")
    @require_roles("triage_lead", "admin")
    def integration_status():
        c = pipeline.connector
        return jsonify(tracker=c.name, label=c.label, configured=c.configured(),
                       last_sync=db.get_setting(f"last_sync_{c.name}"),
                       pending_writes=len(db.failed_syncs()),
                       webhook_url=f"/api/webhooks/{c.name}" if c.name in ("github", "jira") else None,
                       events=db.sync_events(50) if g.user["role"] == "admin" else [])

    @app.post("/api/integration/sync")
    @require_roles("triage_lead", "admin")
    def integration_sync():
        try:
            summary = pipeline.sync()
        except SyncError as exc:
            return jsonify(error=str(exc)), 400
        db.audit(g.user["id"], "tracker_sync", detail=summary)
        return jsonify(summary)

    def simulated():
        if pipeline.name != "simulated":
            raise ValidationError("The simulated tracker is not in use.")
        return pipeline.connector

    @app.get("/api/integration/simulated")
    @require_roles("triage_lead", "admin")
    def simulated_state():
        return jsonify(simulated().state())

    @app.post("/api/integration/simulated/issues")
    @require_roles("triage_lead", "admin")
    def simulated_create():
        data = body()
        issue = simulated().create_issue(text_field(data, "title", 3, "Title"),
                                         text_field(data, "body", 1, "Description"),
                                         str(data.get("reporter") or "demo-reporter")[:50])
        return jsonify(issue), 201

    @app.post("/api/integration/simulated/issues/<key>/reply")
    @require_roles("triage_lead", "admin")
    def simulated_reply(key):
        data = body()
        try:
            simulated().add_reply(key, str(data.get("author") or "reporter")[:50],
                                  text_field(data, "body", 2, "Reply"))
        except SyncError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(ok=True)

    @app.post("/api/integration/simulated/issues/<key>/close")
    @require_roles("triage_lead", "admin")
    def simulated_close(key):
        data = body()
        try:
            simulated().close_externally(key, str(data.get("author") or "developer")[:50],
                                         text_field(data, "body", 2, "Closing comment"))
        except SyncError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(ok=True)

    @app.post("/api/webhooks/<tracker>")
    def webhook(tracker):
        """Called by GitHub or Jira. No session: authenticity comes from the HMAC signature."""
        c = pipeline.connector
        if tracker != c.name or tracker not in ("github", "jira"):
            return jsonify(error="Not found."), 404
        raw = request.get_data()
        if not c.verify_webhook(request.headers, raw, request.args):
            db.log_sync(None, c.name, "in", "webhook_rejected", False, "bad or missing signature")
            return jsonify(error="Invalid signature."), 401
        payload = json.loads(raw or b"{}")
        event = c.parse_webhook(request.headers, payload)
        if not event:
            return jsonify(handled=False)  # other events are acknowledged and ignored
        handled = pipeline.handle_webhook(*event)
        return jsonify(handled=bool(handled))

    return app


def start_background_sync(app, interval):
    """Optional polling, for trackers that cannot reach this server with webhooks."""
    pipeline = app.config["pipeline"]

    def loop():
        while True:
            threading.Event().wait(interval)
            try:
                if pipeline.enabled():
                    pipeline.sync()
            except Exception:
                log.exception("Background sync failed")

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    application = create_app()
    interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "0"))
    if interval > 0:
        start_background_sync(application, interval)
    application.run(debug=False, port=int(os.getenv("PORT", "5000")))
