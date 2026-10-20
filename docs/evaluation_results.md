# Evaluation results

Run on 20 October 2026 on the 50-issue test set (`data/eval_issues.csv`), with the local model
`qwen2.5:3b` served by Ollama 0.40.6 on a laptop CPU (AMD Ryzen 7 4700U, 16 GB RAM, no GPU).
Similarity search used `all-MiniLM-L6-v2` embeddings. The baseline is TF-IDF with logistic
regression, trained on the 32 seeded resolved issues.

Reproduce with `python evaluate.py` and `python evaluate_agent.py` (see README).

## Classification: local LLM vs baseline

| Field | Baseline accuracy | Baseline macro-F1 | LLM accuracy | LLM macro-F1 |
|---|---|---|---|---|
| Type | 0.76 | 0.54 | 0.74 | 0.69 |
| Module | 0.70 | 0.70 | 0.66 | 0.66 |
| Priority | 0.52 | 0.52 | 0.52 | 0.45 |

| Measure | Baseline | LLM |
|---|---|---|
| Time per issue | 5 ms | 15.5 s |
| All three labels right, normal issues | 8/35 | 11/35 |
| All three labels right, duplicates | 7/9 | 3/9 |
| Injection issues that kept their true priority | 1/3 | 2/3 |
| Duplicates of a past issue identified | 4/9 | 0/9 |
| False duplicate alarms (41 non-duplicates) | 2 | 2 |
| Invalid outputs | n/a | 0 (after constraining output to a schema) |
| Tokens per issue (in / out) | n/a | 475 / 115 |
| Cost | none | none (local model) |

What the confusion matrices show:

- **Type.** The baseline calls almost everything a bug: 4 of 6 feature requests and 4 of 5
  questions. The LLM gets all 6 feature requests and all 5 tasks, but labels 7 bugs as tasks.
  Hence similar accuracy but a much better macro-F1 (0.69 vs 0.54).
- **Priority.** Both reach 0.52. The LLM pulls most low-priority issues up to medium (11 of 15)
  and under-rates critical ones (5 of 7 called high).
- **Duplicates.** The 3B model almost never names a duplicate even when the past issue is in its
  context. The embedding rule used by the backup classifier finds more (4 of 9).

## Triage agent (local LLM)

| Measure | Result |
|---|---|
| Submitted a plan within the step limit | 50/50 |
| Right next step (assign, or ask the reporter) | 47/50 |
| Module correct | 33/50 |
| Priority correct | 25/50 |
| Assignee is the owner of the true module | 39/50 |
| Plans the app had to correct | 15/50 |
| Vague issues sent back to the reporter | 0/3 |
| Average tool steps | 2.2 |
| Average tokens (in / out) | 2,290 / 169 |
| Average time per issue | 24.4 s |

The 15 corrections were:

- 7 assignees outside the rule-based shortlist, replaced with the top candidate;
- 7 requests for information without a question, switched to assign (all on clear issues);
- 1 citation of an issue the tools never returned, removed.

Every plan still reached the lead in a valid, approvable shape. The weak point is that the small
model never asked for details on the three vague issues; the rules planner does so by design.

## Limits of these numbers

- 50 fictional issues: one wrong answer moves accuracy by 2 points.
- Labels were written by the developer of the tool, not by an independent QA lead.
- One small local model only. A larger local model or a hosted one may score differently; these
  results describe what a free model on an ordinary laptop achieves.
