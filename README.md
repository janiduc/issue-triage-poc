# AI-assisted software issue triage (proof of concept)

A web application that connects to GitHub Issues or Jira and helps a QA lead triage incoming
issues. An AI model classifies each issue, a **triage agent** investigates it with tools and
**proposes** an action plan, and the lead **approves or edits** that plan before anything is
changed. Approved decisions are written back to the tracker. Developers record fixes, which
close the tracker issue and become knowledge for future suggestions.

## The pipeline

```
 GitHub / Jira / simulated tracker
        │  webhook (signed) or polling ("Import now" / interval)
        ▼
 1. Import ── de-duplicated on (tracker, issue id); secrets redacted on the way in
        ▼
 2. Classify ── LLM with retrieved similar past fixes (RAG)  ──fails──►  backup ML classifier
        ▼
 3. Agent plan ── tool use: search past fixes → open one → rank developers → submit plan
        │          (read-only tools, max 6 steps)                ──fails──►  rules-only plan
        ▼
 4. Human approval ── triage lead edits classification, assignee, reply, labels; or asks the
        │              reporter a question instead. Nothing is written before this step.
        ▼
 5. Write-back ── labels, priority, assignee and reply posted to the tracker via an outbox
        │          (failed writes are kept, shown as "not delivered", and retried)
        ▼
 6. Reporter reply (if asked) ── imported from the tracker; issue re-analysed and re-planned
        ▼
 7. Developer fix ── resolution recorded, tracker issue closed with the fix as a comment
        ▼
 8. Knowledge base ── the fix improves similarity search, recommendations and the ranking
                     (issues closed directly in the tracker are imported too)
```

## Where AI is used, and where it deliberately is not

| Step | Approach | Why |
|---|---|---|
| Classification and priority | LLM + retrieval (RAG) | Unstructured, informal text |
| Duplicate detection, similar fixes | Sentence embeddings | Finds rewordings keywords miss |
| Investigation and action plan | **Tool-using LLM agent** | Multi-step reasoning across past fixes and team data |
| Reply to the reporter | Drafted by the agent | Saves the lead writing time |
| **Assignee ranking** | **Fixed, transparent rules** | Fairness and accountability: people can check the scores |
| **Final decision** | **Human (triage lead)** | The AI proposes; a person decides |

### Assignee ranking (rule-based, human-approved)

`triage/assignment.py` scores each active developer:

| Rule | Points |
|---|---|
| Owns the issue's module | +3 |
| Fixed one of the similar past issues | +2 each (max 2) |
| Past fixes in this module | +0.5 each (max 4) |
| Open assignments right now | −1 each |

The agent can only pick an assignee from this shortlist, and must say why. If it names anyone
else, the choice is replaced with the top-ranked developer and the lead is told. The lead sees
the scores and reasons, and can pick any developer before approving.

### The triage agent

`triage/agent.py` runs a bounded tool-use loop with four read-only tools:
`search_similar_issues`, `get_past_issue`, `rank_developers`, and `submit_plan`. Its plan
contains the classification, next step (assign or ask the reporter), assignee and reason, a
reply or question for the reporter, labels, a possible duplicate, confidence and a one-line
summary. The lead can expand "How the agent reached this plan" to see every tool call.

Safeguards: issue text is fenced as untrusted data; a step limit stops runaway loops; cited
issue ids must come from the agent's own tool results; labels are built from the validated
classification plus a fixed list of optional extras; any failure falls back to a rules-only
plan with the same shape, so triage never stops.

Every approval is compared with the proposal. The Insights screen shows how often plans were
approved unchanged and how often the lead kept the proposed assignee and reply — direct
evidence for evaluating the agent (LO4).

## User roles

| Feature | Reporter | Triage lead | Developer | Admin |
|---|:-:|:-:|:-:|:-:|
| Submit an issue in the desk | ✓ | ✓ | ✓ | |
| Track own issues, see the team's update and the resolution | ✓ | ✓ | ✓ | |
| Answer a request for more information | ✓ | ✓ | ✓ | |
| See AI classification, similar issues, suggested fix | | ✓ | assigned only | |
| See and approve or edit the agent's plan | | ✓ | | |
| Ask the agent to re-plan | | ✓ | | |
| Import from the tracker, resend failed updates | | ✓ | assigned only (resend) | ✓ (import) |
| Record a resolution (closes the tracker issue) | | ✓ | assigned only | |
| Insights: AI and agent agreement, cost, reliability | | ✓ | | ✓ |
| Manage users, roles, modules, tracker account mapping | | | | ✓ |
| Switch the AI model or the agent off; review threshold | | | | ✓ |
| Sync log and activity log | | | | ✓ |

Reporters never see AI internals or other customers' issues. Administrators cannot read issue
content. External reporters (on GitHub or Jira) are answered in the tracker itself.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in the values you need and load them
python app.py
```

Open http://127.0.0.1:5000. Demo accounts all use the password `demo1234`:
`client.acme` and `tester` (reporters), `lead` (triage lead), `dev.payments`, `dev.platform`,
`dev.frontend` (developers), `admin`. Change or remove them outside a demo.

Without an API key everything still works using the backup classifier and the rules planner.

### Connecting an issue tracker

Set `TRACKER` and restart. Secrets are read from environment variables only; they are never
stored in the database or shown in the interface.

**Simulated (default).** A JSON-file tracker with four sample issues. Its Tracker screen lets you
reply as the reporter, close issues as if in the tracker, and create new ones, so the complete
pipeline can be demonstrated offline.

**GitHub Issues.**
1. Create a fine-grained personal access token for the repository with *Issues: read and write*
   and *Metadata: read*.
2. Set `TRACKER=github`, `GITHUB_TOKEN`, `GITHUB_REPO=owner/name`.
3. Optional, for instant imports: in the repository's Settings → Webhooks, add
   `https://<your-server>/api/webhooks/github`, content type `application/json`, a secret
   (also set as `GITHUB_WEBHOOK_SECRET`), and the *Issues* and *Issue comments* events.
   Requests without a valid `X-Hub-Signature-256` are rejected.
4. As admin, enter each developer's GitHub login in Users and roles so assignments appear in GitHub.

**Jira Cloud.**
1. Create an API token at id.atlassian.com for an account with access to the project.
2. Set `TRACKER=jira`, `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT`.
3. Optional: add an admin webhook (Jira settings → System → WebHooks) pointing at
   `https://<your-server>/api/webhooks/jira` with a secret (also `JIRA_WEBHOOK_SECRET`), for
   issue created, issue updated and comment created. Jira signs with `X-Hub-Signature`.
4. Enter each developer's Jira account ID in Users and roles.
5. Priorities map to Jira's default scheme (critical → Highest … low → Low); edit
   `PRIORITY_MAP` in `triage/integrations/jira.py` for custom schemes.

Webhooks need a public URL. For a local demo, use "Import now" or set `SYNC_INTERVAL_SECONDS`.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Enables the LLM and the agent |
| `LLM_MODEL` | `claude-haiku-4-5-20251001` | Model for classification and the agent |
| `SECRET_KEY` | development value | Signs session cookies; set a long random value |
| `TRACKER` | `simulated` | `simulated`, `github`, `jira` or `none` |
| `SYNC_INTERVAL_SECONDS` | `0` | Background polling interval when run with `python app.py` |
| `USE_EMBEDDINGS` | `true` | `false` uses TF-IDF similarity instead |
| `DB_PATH` | `triage.db` | SQLite file; delete to reset data and demo users |

### Deployment

`Dockerfile` builds a container served by gunicorn, with data in a `/data` volume:

```bash
docker build -t triage-desk .
docker run -p 8000:8000 --env-file .env -v triage-data:/data triage-desk
```

Under gunicorn the background poller does not run; use webhooks or call the sync endpoint on a
schedule. `.github/workflows/ci.yml` runs the tests on every push.

## Tests and evaluation

```bash
python -m unittest -v   # 34 tests; AI model, GitHub and Jira are all mocked
python evaluate.py      # LLM vs baseline classifier: accuracy, macro-F1, tokens, cost
```

| Group | What is tested |
|---|---|
| Workflow (TC1–9) | Classification, grounded recommendations, fallbacks, vague input, redaction, prompt injection, end-to-end across roles, request-info round trip |
| Access control (AC1–7) | Each role limited to its features; lockout; admin safeguards |
| Agent (AG1–5) | Tool-use plan, constraints on assignee and citations, step limit fallback, rules plan for vague issues, approval metrics |
| Ranking (RK1) | Ownership and history rank first; workload lowers score |
| Pipeline (PL1–6) | Idempotent import, write-back, reporter reply round trip, closing, knowledge-base import, outbox retry after tracker outage |
| GitHub (GH1–2) | Pull requests skipped, correct write endpoints, signed webhook accepted, unsigned rejected |
| Jira (JR1–3) | ADF conversion, `nextPageToken` pagination, priority mapping, Done transition, missing-transition error |

## Project structure

| File | Role |
|---|---|
| `app.py` | Routes, role checks, approval endpoints, integration and webhook endpoints |
| `triage/service.py` | Classification workflow; creates agent or rules plans |
| `triage/agent.py` | Triage agent (tool-use loop, validation) and rules planner |
| `triage/assignment.py` | Rule-based developer ranking |
| `triage/pipeline.py` | Import, reply and closure handling, outbox write-back |
| `triage/integrations/` | `github.py`, `jira.py`, `simulated.py`, shared `base.py` and `http.py` |
| `triage/llm.py`, `validation.py` | Classification prompt, API call, output validation |
| `triage/similarity.py`, `baseline.py` | Embedding search; backup ML classifier |
| `triage/auth.py`, `redact.py`, `db.py` | Login and roles; secret removal; SQLite storage |
| `static/index.html` | Single-page front end with screens per role |

## Disclosure: external components and simulation

- **External services:** Anthropic Messages API (classification and agent); GitHub REST API;
  Jira Cloud REST API v3.
- **Pretrained model:** `all-MiniLM-L6-v2` sentence embeddings, run locally.
- **Simulated:** the default tracker (`data/simulated_tracker_seed.json`) and both datasets in
  `data/` describe a fictional e-commerce platform. The GitHub and Jira connectors are tested
  against mocked API responses; test them against a real repository or project before relying
  on them.

## Known limitations

- The agent's confidence is self-reported and not calibrated.
- Ranking weights are hand-set; they should be tuned with the team.
- A reporter's reply is detected on the next sync or webhook; there are no email notifications.
- Label writes add labels but do not remove old priority labels when priority changes.
- Single-process design (SQLite, in-memory login lockout); a production version would use a
  database server and a job queue for sync.
- Regex redaction misses names and unusual secret formats.
