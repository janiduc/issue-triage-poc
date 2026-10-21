# Two-repository demo

Run on 21 October 2026 on branch `multi-project`, with a fresh desk database, the local model
`qwen2.5:3b` via Ollama, and two real GitHub repositories:

| Project | Repository | Token (by variable name) | Modules |
|---|---|---|---|
| bluefin-shop (first project, from `.env`) | [janiduc/bluefin-shop](https://github.com/janiduc/bluefin-shop) | `GITHUB_TOKEN` | the default seven |
| Harbour Bookings (added under Projects) | [janiduc/harbour-bookings](https://github.com/janiduc/harbour-bookings) | `GITHUB_TOKEN_HARBOUR` | bookings, payments, notifications, other |

Each token can reach only its own repository (checked: the other repository answers 403), so the
desk really is using a separate connection per project.

## Setup as administrator

- Created the Harbour Bookings project, a lead who leads only Harbour (`lead.harbour`), and added
  `dev.payments` (payments) and `dev.frontend` (bookings, notifications, other) as developers.
- **Test connection** on both projects: both connected. For Harbour it reported that
  `dev.payments` and `dev.frontend` cannot be assigned on GitHub, because their GitHub logins are
  not collaborators of that repository. GitHub accepted the assignment call without assigning
  anyone, so this warning is the only place the problem shows.

## Results

| Check | Result |
|---|---|
| Import, bluefin-shop | 7 issues (issues already labelled by an earlier triage are skipped by design), 380 s |
| Import, Harbour Bookings | 8 issues, 291 s |
| bluefin-shop lead opens a Harbour issue | 404 (not found) |
| bluefin-shop lead lists the Harbour queue | 403 |
| Harbour lead opens a bluefin-shop issue | 404 |
| Harbour classifications use only Harbour's modules | Yes, all 8 |
| Similar issues offered for Harbour issues | None from other projects (Harbour had no fixes yet, and the seed fixes are not shared) |
| Approve Harbour #1 | Labels and reply posted to harbour-bookings with Harbour's token |
| Resolve Harbour #1 | Closed on GitHub with the fix as the last comment |
| Same new issue text in both projects after that fix | Harbour suggests the fix (issue 40); bluefin-shop suggests nothing |

## Findings

1. **Isolation holds** across access, queues, modules, knowledge and tokens.
2. **The small model is weak on a new project with no history**: it put 6 of 8 Harbour issues in
   "bookings" (the deposit and discount bugs belong in payments, the email bug in
   notifications) and rated a feature request and a question as critical. The lead corrects these
   before approval, and each confirmed decision trains Harbour's own classifier.
3. **The agent's reply on Harbour #1 asked the reporter for steps** although the plan assigned the
   issue, and it was approved unedited. The prompt already forbids this; the small model ignores
   it. The lead must read the reply before approving; a stricter automatic check is a possible
   next step.
