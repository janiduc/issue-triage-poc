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
from werkzeug.exceptions import Forbidden, HTTPException, NotFound

from triage import config, db
from triage.auth import (authenticate, current_user, effective_roles, project_role, require_roles,
                         reset_failed_logins)
from triage.agent import EXTRA_LABELS
from triage.integrations.base import SyncError
from triage.pipeline import Pipelines
from triage.service import TriageService, agent_enabled, ai_enabled, confidence_threshold, to_api
from triage.validation import ValidationError, validate_issue_input

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("triage")

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
TRACKERS = ("github", "jira", "simulated", "none")
PROJECT_ROLES = ("triage_lead", "developer", "reporter")


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


def proposed_assign_comment(plan):
    """Reply the plan proposes if the issue is assigned (older plans lack 'assign_comment')."""
    return plan.get("assign_comment") or plan.get("comment_for_reporter")


def view_role_for(issue, user):
    """Which view of an issue this user may see: 'full', 'reporter', or None (no access).
    It depends on the user's role in the issue's project; people always keep a read-only view
    of issues they reported, even after leaving the project."""
    role = project_role(user, issue["project_id"])
    if role == "triage_lead":
        return "full"
    if role == "developer" and issue["assignee_id"] == user["id"]:
        return "full"
    if role != "admin" and issue["reporter_id"] == user["id"]:
        return "reporter"
    return None


def create_app():
    config.check_production_settings()
    app = Flask(__name__, static_folder="static")
    app.config.update(SECRET_KEY=config.secret_key(), SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      # Production is served over HTTPS, so the cookie must never travel in clear.
                      SESSION_COOKIE_SECURE=config.is_production())
    if not config.is_production() and config.secret_key() in config.DEFAULT_SECRET_KEYS:
        log.warning("Using the default SECRET_KEY: fine for a local demo, never for a deployment.")
    db.init_db()
    service = TriageService()
    db.sync_env_project()
    pipelines = Pipelines(service)
    # "pipeline" = the first project's, kept for single-project callers and tests
    app.config.update(service=service, pipelines=pipelines, pipeline=pipelines.default())

    def scoped_project(*roles):
        """The project named by ?project=<slug> (or "project" in the body), which the user must
        hold one of `roles` in (administrators may use any). Without a name: the user's first
        such project."""
        slug = request.args.get("project") or body().get("project")
        if slug:
            project = db.get_project_by_slug(str(slug))
            if not project:
                raise NotFound("Project not found.")
        else:
            if g.user["role"] == "admin":
                project = db.get_project(db.default_project_id())
            else:
                ids = [pid for pid, m in sorted(g.user["projects"].items()) if m["role"] in roles]
                project = db.get_project(ids[0]) if ids else None
            if not project:
                raise Forbidden("You are not in a project with that role.")
        if g.user["role"] != "admin" and project_role(g.user, project["id"]) not in roles:
            raise Forbidden("Your role in this project does not allow that.")
        return project

    def selected_pipeline():
        return pipelines.get(scoped_project("triage_lead")["id"])

    def require_project_role(issue, *roles):
        """404 (not 403) when the user lacks the role in the issue's project, so it stays hidden."""
        if project_role(g.user, issue["project_id"]) not in roles:
            raise NotFound("Issue not found.")

    def load_issue_for(issue_id):
        issue = db.get_issue(issue_id)
        if not issue or issue["is_seed"]:
            return None, None
        return issue, view_role_for(issue, g.user)

    def issue_response(issue_id, status=200):
        issue = db.get_issue(issue_id)
        view = view_role_for(issue, g.user)
        role = project_role(g.user, issue["project_id"])
        data = to_api(issue, "reporter" if view == "reporter" else role)
        project = db.get_project(issue["project_id"])
        data["project"] = {"slug": project["slug"], "name": project["name"], "modules": project["modules"]}
        data["my_role"] = role
        if role == "triage_lead":
            data["plan"] = db.latest_plan(issue_id)
        if view == "full":
            p = pipelines.for_issue(issue)
            data["tracker_label"] = p.connector.label if p and issue["source"] == p.name else None
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
            expected = proposed.get(field)
            if field == "comment_for_reporter" and final.get("action") == "assign":
                expected = proposed_assign_comment(proposed)
            if field in final and final[field] != expected:
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

    @app.get("/api/public-config")
    def public_config():
        """What the sign-in page may show before anyone signs in."""
        demo = db.get_user_by_username("lead") is not None and config.seed_demo_users()
        return jsonify(demo_accounts=demo)

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
        if g.user["role"] == "admin":
            projects = [{"slug": p["slug"], "name": p["name"], "role": "admin"}
                        for p in db.list_projects(active_only=True)]
        else:
            projects = [{"slug": p["slug"], "name": p["name"], "role": g.user["projects"][p["id"]]["role"]}
                        for p in db.list_projects(active_only=True) if p["id"] in g.user["projects"]]
        return jsonify({k: v for k, v in g.user.items() if k != "projects"} | {"projects": projects})

    @app.get("/api/options")
    @require_roles()
    def options():
        slug = request.args.get("project")
        project = db.get_project_by_slug(slug) if slug else None
        return jsonify(types=config.ISSUE_TYPES, modules=(project or {}).get("modules") or config.MODULES,
                       priorities=config.PRIORITIES, roles=config.ROLES,
                       role_labels=config.ROLE_LABELS)

    @app.get("/api/health")
    @require_roles("triage_lead", "admin")
    def health():
        return jsonify(llm_enabled=ai_enabled(), agent_enabled=agent_enabled(),
                       api_key_configured=config.llm_enabled(),
                       provider=config.llm_provider(), model=config.llm_model(), similarity_method=service.index.method,
                       knowledge_base_size=len(service.index.rows))

    # ---------- issues ----------

    @app.post("/api/issues")
    @require_roles("reporter", "developer", "triage_lead")
    def create_issue():
        data = validate_issue_input(request.get_json(silent=True))
        slug = body().get("project")
        member_of = sorted(g.user["projects"])
        if slug:
            project = db.get_project_by_slug(str(slug))
            if not project or project["id"] not in g.user["projects"] or not project["active"]:
                raise ValidationError("Choose a project you belong to.")
            project_id = project["id"]
        elif member_of:
            project_id = member_of[0]
        else:
            raise ValidationError("You are not a member of any project yet. Ask an administrator.")
        issue_id = service.triage_new(data["title"], data["description"], g.user["id"], project_id=project_id)
        db.audit(g.user["id"], "issue_submitted", issue_id)
        return issue_response(issue_id, 201)

    @app.get("/api/issues")
    @require_roles("reporter", "developer", "triage_lead")
    def list_issues():
        roles = effective_roles(g.user)
        default_scope = ("all" if "triage_lead" in roles else "assigned" if "developer" in roles else "mine")
        scope = request.args.get("scope", default_scope)
        allowed = {"mine"} | ({"all"} if "triage_lead" in roles else set()) \
            | ({"assigned"} if "developer" in roles else set())
        if scope not in allowed:
            return jsonify(error="Your role cannot view that list."), 403
        status = request.args.get("status") or None
        if status and status not in config.ISSUE_STATUSES:
            raise ValidationError("Unknown status.")
        if scope == "all":  # one project's queue, for its triage lead
            rows = db.list_issues(status=status, project_id=scoped_project("triage_lead")["id"])
            out = [to_api(r, "triage_lead") for r in sort_queue(rows)]
        elif scope == "assigned":  # across every project where the user develops
            rows = [r for r in db.list_issues(status=status, assignee_id=g.user["id"])
                    if project_role(g.user, r["project_id"]) == "developer"]
            out = [to_api(r, "developer") for r in sort_queue(rows)]
        else:
            out = [to_api(r, "reporter") for r in sort_queue(db.list_issues(status=status,
                                                                            reporter_id=g.user["id"]))]
        return jsonify(out)

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
        require_project_role(issue, "triage_lead")
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
            for field, allowed in (("type", config.ISSUE_TYPES), ("module", service.modules_for(issue["project_id"])),
                                   ("priority", config.PRIORITIES)):
                if data.get(field):
                    if data[field] not in allowed:
                        raise ValidationError(f"Invalid {field}.")
                    final[field] = data[field]
        assignee_id = data.get("assignee_id") or (proposed or {}).get("assignee_id")
        dev = db.get_user(assignee_id) if assignee_id else None
        dev_role = db.memberships(dev["id"]).get(issue["project_id"], {}).get("role") if dev else None
        if not dev or dev_role != "developer" or not dev["active"]:
            raise ValidationError("Choose an active developer of this project to assign.")
        comment = data.get("comment")
        if comment is None and proposed:
            comment = proposed_assign_comment(proposed)
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
            p = pipelines.for_issue(issue)
            write = p.triage_write(db.get_issue(issue_id), extras, comment)
            p.queue(issue_id, "triage", **write)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/request-info")
    @require_roles("triage_lead")
    def request_info(issue_id):
        issue, _ = load_issue_for(issue_id)
        if not issue:
            return jsonify(error="Issue not found."), 404
        require_project_role(issue, "triage_lead")
        if issue["status"] != "new":
            raise ValidationError("You can only ask for information on issues awaiting triage.")
        message = text_field(body(), "message", 5, "Your question")
        db.request_info(issue_id, message)
        record_plan_decision(issue_id, {"action": "request_info", "question_for_reporter": message})
        db.audit(g.user["id"], "info_requested", issue_id)
        pipelines.for_issue(issue).queue(issue_id, "comment", text=message)
        return issue_response(issue_id)

    @app.post("/api/issues/<int:issue_id>/replan")
    @require_roles("triage_lead")
    def replan(issue_id):
        issue, _ = load_issue_for(issue_id)
        if issue:
            require_project_role(issue, "triage_lead")
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
        pipelines.for_issue(issue).deliver(issue_id)
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
        service.refresh(issue["project_id"])  # the fix now helps with similar issues in this project
        if body().get("close_in_tracker", True):
            pipelines.for_issue(issue).queue(issue_id, "close", text=f"Resolved: {resolution}")
        return issue_response(issue_id)

    @app.get("/api/developers")
    @require_roles("triage_lead")
    def developers():
        project = scoped_project("triage_lead")
        return jsonify([{"id": d["id"], "name": d["display_name"], "modules": d["modules"],
                         "open_count": d["open_count"]} for d in db.developer_workload(project["id"])])

    # ---------- insights ----------

    @app.get("/api/metrics")
    @require_roles("triage_lead", "admin")
    def metrics():
        # A lead sees their project; an administrator sees one project (?project=) or the whole desk.
        if g.user["role"] == "admin" and not request.args.get("project"):
            project_id = None
        else:
            project_id = scoped_project("triage_lead")["id"]
        rows = db.list_issues(project_id=project_id)
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
            # Lead's final labels vs the first-pass classifier (LLM or backup), not the plan.
            agreement={f: agreement(f) for f in ("type", "module", "priority")},
            avg_latency_ms=avg([r["latency_ms"] for r in rows]),
            avg_tokens_in=avg([r["tokens_in"] for r in llm_rows]),
            avg_tokens_out=avg([r["tokens_out"] for r in llm_rows]),
            agent=agent_metrics(project_id),
            sync_failures=len(db.failed_syncs(project_id)),
        )

    def agent_metrics(project_id=None):
        plans = db.plan_rows(project_id)
        decided = [p for p in plans if p["status"] in ("approved", "edited")]
        agent_plans = [p for p in plans if p["source"] == "agent"]

        def changed(p, field):
            return field in json.loads(p["decision_json"] or "{}").get("changed", [])

        def kept(field):
            return round(sum(not changed(p, field) for p in decided) / len(decided), 2) if decided else None

        return {
            "plans": len(plans), "by_agent": len(agent_plans),
            "by_rules": sum(p["source"] == "rules" for p in plans),
            "decided": len(decided),
            "approved_unchanged": sum(p["status"] == "approved" for p in decided),
            "assignee_kept": kept("assignee_id"),
            "comment_kept": kept("comment_for_reporter"),
            # Lead vs the PLAN's classification (agent or rules). The top-level "agreement"
            # is lead vs the first-pass classifier; the two differ when the agent revises it.
            "classification_kept": {f: kept(f) for f in ("type", "module", "priority")},
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
        if not isinstance(modules, list) or any(m not in service.modules_for(db.default_project_id())
                                                for m in modules):
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
                       api_key_configured=config.llm_enabled(), provider=config.llm_provider(),
                       model=config.llm_model(), confidence_threshold=confidence_threshold())

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
        try:
            limit = int(request.args.get("limit", 100))
        except ValueError:
            raise ValidationError("limit must be a whole number.")
        limit = max(1, min(limit, 500))
        slug = request.args.get("project")
        project = db.get_project_by_slug(slug) if slug else None
        if slug and not project:
            raise NotFound("Project not found.")
        return jsonify(db.audit_entries(limit, project["id"] if project else None))

    # ---------- projects (administrator) ----------

    def project_or_404(slug):
        project = db.get_project_by_slug(slug)
        if not project:
            raise NotFound("Project not found.")
        return project

    def clean_project(data, existing=None):
        """Validate project fields. Secrets are never accepted: only env variable NAMES."""
        out = {}
        if "name" in data or not existing:
            out["name"] = text_field(data, "name", 2, "Project name")[:100]
        tracker = data.get("tracker", (existing or {}).get("tracker", "none"))
        if tracker not in TRACKERS:
            raise ValidationError("Tracker must be github, jira, simulated or none.")
        out["tracker"] = tracker
        for key in ("repo", "base_url", "token_env", "email_env", "secret_env"):
            if key in data:
                out[key] = (str(data[key] or "").strip() or None)
        merged = {**(existing or {}), **out}
        for key in ("token_env", "email_env", "secret_env"):
            if merged.get(key) and not ENV_NAME_RE.match(merged[key]):
                raise ValidationError("Secrets are referenced by environment variable name, "
                                      "for example GITHUB_TOKEN_SHOP (capitals, digits, underscores).")
        if tracker == "github" and not REPO_RE.match(merged.get("repo") or ""):
            raise ValidationError("GitHub repository must be owner/name.")
        if tracker == "jira" and not (str(merged.get("base_url") or "").startswith("https://")
                                      and merged.get("repo")):
            raise ValidationError("Jira needs an https:// site URL and a project key.")
        if "modules" in data or not existing:
            modules = data.get("modules") or config.MODULES
            if (not isinstance(modules, list) or not 1 <= len(modules) <= 20
                    or any(not MODULE_RE.match(str(m)) for m in modules) or len(set(modules)) != len(modules)):
                raise ValidationError("Modules: 1 to 20 distinct names of lowercase letters, digits or _.")
            out["modules"] = modules
        if "active" in data:
            out["active"] = int(bool(data["active"]))
        if "share_seed" in data:
            out["share_seed"] = int(bool(data["share_seed"]))
        return out

    def project_summary(project):
        p = pipelines.get(project["id"])
        return project | db.project_counts(project["id"]) | {
            "configured": bool(p and p.connector.configured()),
            "webhook_url": f"/api/webhooks/{project['slug']}" if project["tracker"] in ("github", "jira") else None}

    @app.get("/api/admin/projects")
    @require_roles("admin")
    def admin_projects():
        return jsonify([project_summary(p) for p in db.list_projects()])

    @app.post("/api/admin/projects")
    @require_roles("admin")
    def admin_create_project():
        data = body()
        slug = str(data.get("slug") or "").strip().lower()
        if not SLUG_RE.match(slug):
            raise ValidationError("Short name: 2 to 40 lowercase letters, digits or dashes.")
        if db.get_project_by_slug(slug):
            raise ValidationError("A project with that short name already exists.")
        fields = clean_project(data)
        project_id = db.create_project(slug, fields.pop("name"), fields.pop("tracker"), fields.pop("modules"),
                                       **{k: v for k, v in fields.items() if k != "active"})
        db.audit(g.user["id"], "project_created", detail={"project": slug})
        return jsonify(project_summary(db.get_project(project_id))), 201

    @app.patch("/api/admin/projects/<slug>")
    @require_roles("admin")
    def admin_update_project(slug):
        project = project_or_404(slug)
        fields = clean_project(body(), project)
        tracker_keys = {"tracker", "repo", "base_url", "token_env", "email_env", "secret_env"}
        if any(k in body() for k in tracker_keys):
            fields["managed_by_env"] = 0  # edited by an administrator: stop following .env
        db.update_project(project["id"], **fields)
        if {"modules", "share_seed"} & set(fields):
            service.refresh(project["id"])
        db.audit(g.user["id"], "project_updated", detail={"project": slug, "changed": sorted(body())})
        return jsonify(project_summary(db.get_project(project["id"])))

    @app.post("/api/admin/projects/<slug>/test")
    @require_roles("admin")
    def admin_test_project(slug):
        project = project_or_404(slug)
        result = pipelines.get(project["id"]).connector.check()
        members = db.project_members(project["id"])
        if result["ok"] and project["tracker"] == "github":
            logins = {a.lower() for a in result["assignable"]}
            result["unassignable"] = [m["username"] for m in members if m["project_role"] == "developer"
                                      and (m.get("github_login") or "").lower() not in logins]
        db.audit(g.user["id"], "project_tested", detail={"project": slug, "ok": result["ok"]})
        return jsonify(result)

    @app.get("/api/admin/overview")
    @require_roles("admin")
    def admin_overview():
        """Per project: who holds which role, and each person's open and resolved assignments.
        Counts only; no issue content."""
        slug = request.args.get("project")
        projects = [project_or_404(slug)] if slug else db.list_projects()
        out = []
        for project in projects:
            members = db.project_members(project["id"])
            counts = db.assignment_counts(project["id"])
            by_status = db.project_counts(project["id"])["by_status"]
            people = [{"user_id": m["id"], "display_name": m["display_name"], "username": m["username"],
                       "role": m["project_role"], "modules": m["project_modules"],
                       "github_login": m.get("github_login"), "active": m["active"],
                       **counts.get(m["id"], {"open": 0, "resolved": 0})} for m in members]
            # People who still hold issues here but are no longer members (should be none)
            orphans = [uid for uid, c in counts.items() if c["open"] and uid not in {m["id"] for m in members}]
            out.append({"slug": project["slug"], "name": project["name"], "tracker": project["tracker"],
                        "repo": project["repo"], "active": project["active"],
                        "roles": {r: sum(p["role"] == r for p in people) for r in PROJECT_ROLES},
                        "members": people, "unassigned_open": by_status.get("new", 0)
                        + by_status.get("needs_info", 0), "assigned_open": by_status.get("assigned", 0),
                        "resolved": by_status.get("resolved", 0), "orphan_assignees": len(orphans)})
        return jsonify(out)

    @app.get("/api/admin/projects/<slug>/members")
    @require_roles("admin")
    def admin_project_members(slug):
        project = project_or_404(slug)
        return jsonify([{"user_id": m["id"], "username": m["username"], "display_name": m["display_name"],
                         "role": m["project_role"], "modules": m["project_modules"],
                         "github_login": m.get("github_login"), "active": m["active"]}
                        for m in db.project_members(project["id"])])

    def clean_member(project, data, role=None):
        role = data.get("role", role)
        if role not in PROJECT_ROLES:
            raise ValidationError("Role must be triage lead, developer or reporter.")
        modules = data.get("modules") or []
        if role != "developer":
            modules = []
        elif not isinstance(modules, list) or any(m not in project["modules"] for m in modules):
            raise ValidationError("Unknown module for this project.")
        return role, modules

    def last_lead(project, user_id):
        leads = [m for m in db.project_members(project["id"]) if m["project_role"] == "triage_lead"]
        return project["active"] and len(leads) == 1 and leads[0]["id"] == user_id

    @app.post("/api/admin/projects/<slug>/members")
    @require_roles("admin")
    def admin_add_member(slug):
        project = project_or_404(slug)
        data = body()
        user = db.get_user(data.get("user_id")) if data.get("user_id") else None
        if not user or user["role"] == "admin":
            raise ValidationError("Choose a user who is not an administrator.")
        if user["id"] in {m["id"] for m in db.project_members(project["id"])}:
            raise ValidationError("That user is already a member; change their role instead.")
        role, modules = clean_member(project, data)
        db.set_membership(project["id"], user["id"], role, modules)
        db.audit(g.user["id"], "member_added", detail={"project": slug, "user_id": user["id"], "role": role})
        return admin_project_members(slug)

    @app.patch("/api/admin/projects/<slug>/members/<int:user_id>")
    @require_roles("admin")
    def admin_update_member(slug, user_id):
        project = project_or_404(slug)
        current = db.memberships(user_id).get(project["id"])
        if not current:
            raise NotFound("Not a member of this project.")
        role, modules = clean_member(project, body(), current["role"])
        if current["role"] == "triage_lead" and role != "triage_lead" and last_lead(project, user_id):
            raise ValidationError("This is the project's only triage lead. Add another lead first.")
        if current["role"] == "developer" and role != "developer" and db.open_assignments(project["id"], user_id):
            raise ValidationError("This developer still has open assignments in the project; "
                                  "reassign them or remove the member with release.")
        db.set_membership(project["id"], user_id, role, modules)
        db.audit(g.user["id"], "member_role_changed", detail={
            "project": slug, "user_id": user_id, "from": current["role"], "to": role})
        return admin_project_members(slug)

    @app.delete("/api/admin/projects/<slug>/members/<int:user_id>")
    @require_roles("admin")
    def admin_remove_member(slug, user_id):
        """Remove one membership. The user and their other projects are untouched."""
        project = project_or_404(slug)
        current = db.memberships(user_id).get(project["id"])
        if not current:
            raise NotFound("Not a member of this project.")
        if current["role"] == "triage_lead" and last_lead(project, user_id):
            raise ValidationError("This is the project's only triage lead. Add another lead first.")
        open_ids = db.open_assignments(project["id"], user_id)
        if open_ids and request.args.get("release") != "true":
            return jsonify(error="This developer still has open assignments in the project.",
                           open_issue_count=len(open_ids)), 409
        if open_ids:
            db.release_assignments(project["id"], user_id)
        db.remove_membership(project["id"], user_id)
        db.audit(g.user["id"], "member_removed", detail={
            "project": slug, "user_id": user_id, "role": current["role"], "released": len(open_ids)})
        return admin_project_members(slug)

    # ---------- tracker integration ----------

    @app.get("/api/integration")
    @require_roles("triage_lead", "admin")
    def integration_status():
        p = selected_pipeline()
        c, project = p.connector, db.get_project(p.project_id)
        return jsonify(project=project["slug"], project_name=project["name"],
                       projects=[{"slug": x["slug"], "name": x["name"], "tracker": x["tracker"]}
                                 for x in db.list_projects(active_only=True)],
                       tracker=c.name, label=c.label, configured=c.configured(),
                       last_sync=project.get("last_sync"),
                       pending_writes=len(db.failed_syncs(p.project_id)),
                       webhook_url=f"/api/webhooks/{project['slug']}" if c.name in ("github", "jira") else None,
                       events=db.sync_events(50, p.project_id) if g.user["role"] == "admin" else [])

    @app.post("/api/integration/sync")
    @require_roles("triage_lead", "admin")
    def integration_sync():
        p = selected_pipeline()
        try:
            summary = p.sync()
        except SyncError as exc:
            return jsonify(error=str(exc)), 400
        db.audit(g.user["id"], "tracker_sync", detail=summary | {"project": p.project["slug"]})
        return jsonify(summary)

    def simulated():
        p = selected_pipeline()
        if p.name != "simulated":
            raise ValidationError("The simulated tracker is not in use.")
        return p.connector

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

    def webhook_pipeline(slug):
        """/api/webhooks/<project slug>. The old /api/webhooks/github (or /jira) still works while
        exactly one active project uses that tracker."""
        p = pipelines.by_slug(slug)
        if p is None and slug in ("github", "jira"):
            matches = [x for x in db.list_projects(active_only=True) if x["tracker"] == slug]
            p = pipelines.get(matches[0]["id"]) if len(matches) == 1 else None
        if p is None or not p.project["active"] or p.name not in ("github", "jira"):
            return None
        return p

    @app.post("/api/webhooks/<slug>")
    def webhook(slug):
        """Called by GitHub or Jira. No session: authenticity comes from the HMAC signature."""
        pipeline = webhook_pipeline(slug)
        if pipeline is None:
            return jsonify(error="Not found."), 404
        c = pipeline.connector
        raw = request.get_data()
        if not c.verify_webhook(request.headers, raw, request.args):
            db.log_sync(None, c.name, "in", "webhook_rejected", False, "bad or missing signature",
                        pipeline.project_id)
            return jsonify(error="Invalid signature."), 401
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return jsonify(error="Webhook body must be JSON."), 400
        if not isinstance(payload, dict):
            return jsonify(error="Webhook body must be a JSON object."), 400
        event = c.parse_webhook(request.headers, payload)
        if not event:
            return jsonify(handled=False)  # other events are acknowledged and ignored
        handled = pipeline.handle_webhook(*event)
        return jsonify(handled=bool(handled))

    return app


def start_background_sync(app, interval):
    """Optional polling, for trackers that cannot reach this server with webhooks."""
    pipelines = app.config["pipelines"]

    def loop():
        while True:
            threading.Event().wait(interval)
            for pipeline in pipelines.all():  # one failing project never stops the others
                try:
                    if pipeline.enabled():
                        pipeline.sync()
                except Exception:
                    log.exception("Background sync failed for project %s", pipeline.project["slug"])

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    config.load_env_file()
    application = create_app()
    interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "0"))
    if interval > 0:
        start_background_sync(application, interval)
    application.run(debug=False, port=int(os.getenv("PORT", "5000")))
