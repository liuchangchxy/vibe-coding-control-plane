# EasyExam Reference Evidence

EasyExam (`liuchangchxy/easy-exam`) is the production reference read for Control Plane v0. Evidence below was read from GitHub on 2026-10-06. EasyExam's application checks and stack are evidence about that repository only.

## Source snapshot

- `main` HEAD read: `c431137bc04a24626122ae87df6db51ceabcf7ba`.
- `docs/automation/AUTOMATION.md` blob at that snapshot: `2f303f8176bd9c7a3f9c50e3d9728cc32ffd98d8`.
- The live Automation v1 document records `AUTONOMOUS_PRODUCTION_LOOP_VALIDATED = YES`, `POST_CLEANUP_SMOKE_VALIDATED = YES`, and `AUTONOMOUS_REPAIR_LOOP_VALIDATED = YES`.

## GitHub evidence

- [Issue #26 / PR #27](https://github.com/liuchangchxy/easy-exam/issues/26) — happy-path documentation validation. PR #27 is merged; the observed required checks and formal approval were tied to head `e83151f7214d921396e17d5972231d7dae4266fd`. Its historical check list also contained a successful, non-required `Reviewer Wake` run; that transport was subsequently removed by PR #29.
- [PR #29](https://github.com/liuchangchxy/easy-exam/pull/29) — merged cleanup titled “chore: remove obsolete reviewer wake transport.” Current Automation v1 documentation says the Reviewer Wake job, SHA marker comment, and comment-based wake are not used. Current `ci.yml` contains only project CI jobs and no Reviewer Wake job.
- [Issue #30 / PR #31](https://github.com/liuchangchxy/easy-exam/issues/30) — post-cleanup smoke path. PR #31 is merged with native Auto-merge configured and a native formal approval. Issue #30 is closed.
- [Issue #32 / PR #33](https://github.com/liuchangchxy/easy-exam/issues/32) — same-PR repair path. PR #33 has a formal `CHANGES_REQUESTED` on SHA `2c9a227d86cf72cb9982c276ab58f1db60515786`, then a formal `APPROVED` on repaired SHA `408c49ab189cfe4ed8b039de5e6045c4f032b4c5`, and merged as `2541c4b3cee23dab70c3d2cb0c3973efccc420f7`. The observed checks on the repaired SHA all concluded success.
- [PR #25](https://github.com/liuchangchxy/easy-exam/pull/25) — native `synchronize` Reviewer transport POC; closed without merge. The current production document names `opened`, `ready_for_review`, and `synchronize` as native PR events.

These artifacts support the happy path, post-cleanup smoke path, exact-SHA formal review, and same-PR repair. They do not establish that a Sidecar/Dispatcher is strictly event-driven. The Control Plane does not claim that it is.

## What is not universal

EasyExam's five required CI jobs are its own gates: Whitespace & Guard Checks; Backend & Packaging Tests; Frontend Unit & Build Tests; Browser E2E Tests; Mobile Interaction E2E. Consumers define their own required checks. No EasyExam Python, FPK, Vue, Playwright, fnOS, or job-name requirement is copied into the Control Plane contract.
