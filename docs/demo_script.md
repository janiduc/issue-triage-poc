# Demo script

A fixed walk-through that shows every pipeline step and one fallback in about ten minutes. It
runs offline on the simulated tracker, so it does not depend on GitHub, an API key or a network.

## Before the demo

1. Start from clean data so the numbers are predictable:
   - stop the app, delete `triage.db` and `simulated_tracker.json` (both are recreated on start);
   - in `.env`, set `TRACKER=simulated`.
2. Choose the model:
   - **With AI:** `LLM_PROVIDER=ollama` and Ollama running with the model pulled (see README), or
     `ANTHROPIC_API_KEY` set.
   - **Backup rehearsal:** no model configured. Everything below still works with the backup
     classifier and the rules planner; only the agent trace in step 4 is missing.
3. `python app.py` and open http://127.0.0.1:5000.

## The walk-through

| # | Sign in as | Do | Point out |
|---|---|---|---|
| 1 | `client.acme` | Report an issue: "Checkout fails with discount code" with a short description that includes an email address | Reporters see only their own issues and status, never AI internals; the email is redacted before anything is stored or sent to the model |
| 2 | `lead` | Triage queue: press **Import from Simulated tracker** | Four tracker issues arrive; importing twice adds nothing (de-duplication) |
| 3 | `lead` | Open SIM-1 (checkout hangs) | Priority, type and module with the model's justification; similar past fixes; the card number in the text was redacted |
| 4 | `lead` | Expand **How the agent reached this plan** | Each tool call: search past fixes, rank developers, submit plan; the assignee comes from the rule-based shortlist with visible scores |
| 5 | `lead` | Change the priority, keep the assignee, edit the reply, **Approve and assign** | Nothing was written before this click |
| 6 | `lead` | Tracker screen | SIM-1 now has labels, priority, assignee and the reply as a comment |
| 7 | `lead` | Open SIM-3 ("Error", vague) and choose **Ask the reporter for details first**, send the question | The rules or the agent suggested asking because the text is too thin |
| 8 | `lead` | Tracker screen: **Reply as reporter** on SIM-3 with real details, then **Import now** | The reply is imported, the issue is re-analysed and re-planned, and is back in the queue |
| 9 | the developer assigned in step 5 (normally `dev.payments`) | My assignments: open SIM-1, record a resolution, **Mark as resolved** | The tracker issue closes with the fix as a comment; the fix joins the knowledge base |
| 10 | `lead` | Report a new issue that rewords SIM-1 | The resolved SIM-1 is now the top similar issue with its fix |
| 11 | `admin` | AI settings: switch the AI model off; report another issue as `lead` | The backup classifier handles it and the issue is flagged "Produced by the backup classifier" (the fallback) |
| 12 | `admin` | Insights, Activity log, Sync log | Agreement with the classifier and with the plan, cost and reliability, and who did what; the admin never sees issue text |

## If something goes wrong

- **The model is slow or down:** say so, and continue. Every step falls back to the backup
  classifier and the rules planner, which is itself part of the design to show.
- **Port 5000 is busy:** an earlier copy is still running; use it, or stop it with Ctrl+C in its
  window.
