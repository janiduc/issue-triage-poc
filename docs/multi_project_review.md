# Multi-project review: problems between projects

Review of branch `multi-project` on 21 October 2026, after the two-repository demo. Problems
marked **reproduced** were confirmed with a throwaway database and mocked trackers; the others come
from reading the code.

**Status, 21 October 2026:** problems 1 to 7 and 9 are fixed on the same branch, each with a
regression test (`CrossProjectFixTests` in `tests/test_projects.py`, RV1 to RV9). Problems 8 and
10 remain open (Low).

## Problems

| # | Problem | Effect | Evidence | Severity |
|---|---|---|---|---|
| 1 | The first project's tracker follows `.env` on every start until an admin edits it. Your `.env` now has two `GITHUB_REPO` lines (bluefin-shop, then harbour-bookings); only the first counts today, but reordering or editing them silently points the first project at another repository. | Issues from the new repository collide with already-imported ones of the same number (#1 matches the old #1), so they are never imported, and replies or closures land on the wrong desk issue. | Code reading (`db.sync_env_project`, `db.find_external`) | High |
| 2 | Archiving a project does not stop manual imports or write-back. | An archived project keeps importing on "Import now" and keeps posting labels and comments to its repository. | **Reproduced**: 4 issues imported and a write-back delivered after archiving | High |
| 3 | Reporting without choosing a project uses the reporter's first membership even if that project is archived. | New issues go into a project nobody is working. | **Reproduced**: issue created in the archived project | Medium |
| 4 | Saving a user on the **Users and roles** screen mirrors their global role into the first project. | Someone removed from the first project is silently added back by an unrelated edit (for example a new display name or GitHub login). | **Reproduced**: removed, renamed, member again | High |
| 5 | The old webhook URL `/api/webhooks/github` only works while exactly one project uses GitHub. | Adding a second GitHub project breaks any webhook still using the old URL (it starts answering 404). | **Reproduced**: 200 with one GitHub project, 404 with two | Medium |
| 6 | Developer workload is counted per project only. | A developer with open work in one project looks free in another and keeps getting assigned there. | **Reproduced**: 3 open issues in the first project, counted as 0 extra in the second | Medium |
| 7 | When a removed developer's issues are sent back to the queue, the old decided plan stays attached. | The lead sees a stale plan naming the removed developer, and the next approval is not recorded in the agent-agreement metrics because the plan is no longer "proposed". | **Reproduced**: status back to "new", plan still "edited" with the old assignee | Medium |
| 8 | The Users and roles screen shows one global role per person, although roles are now per project. | An administrator can misread who leads or develops where (the Team overview is correct). | Code reading | Low |
| 9 | "My assignments" and "My issues" mix all projects with no project label on each row. | A developer in two projects cannot tell which repository an item belongs to until opening it. | Code reading (`listItem` in `static/index.html`) | Low |
| 10 | A person has one GitHub login for all projects. | Someone who uses different accounts in different organisations cannot be assigned correctly in both. | Code reading | Low |

## Accepted by design (not bugs)

- The same problem reported in two repositories is never linked as a duplicate, because knowledge is isolated per project.
- `evaluate.py` and `evaluate_agent.py` measure the shared 50-issue test set, not each project.

## Suggested fixes

1. Stop the first project following `.env` once any second project exists (or once it has imported issues), and refuse to change a project's repository after it has imported issues.
2. Make `scoped_project` and `Pipeline.queue` / `deliver` refuse archived projects; show the archived state in the queue.
3. Only default reporting to an active project; otherwise ask the reporter to pick one.
4. Stop `update_user` mirroring roles into the first project except for the role and modules fields themselves, and label that screen's role as "first project".
5. Keep the old webhook URL working by remembering which project it was first registered for, or document that each project must use `/api/webhooks/<short name>`.
6. Count open assignments across all projects in the ranking (shown as "3 open, 2 in other projects").
7. Re-plan the issue when it is sent back to the queue.
8. Show per-project roles on Users and roles (or link to the Team overview).
9. Add the project name to each row of the cross-project lists.
10. Optional: per-project tracker account mapping on the membership.
