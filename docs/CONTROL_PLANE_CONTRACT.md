# Control Plane Contract v2 with Consumer Onboarding v2

This document defines the cross-repository automation contract. GitHub Issues, Pull Requests, labels, check runs, reviews, and merge state are the durable source of truth. Chat transcripts and a task's prior runs are never authoritative.

## Authority

- **Human** freezes the Issue specification, resolves terminal stops, and owns decisions beyond the frozen scope.
- **Dispatcher** is the sole automated repair admission authority. It detects actionable causes, deduplicates repairs, enforces the shared budget, and moves the Issue to `needs-human` when another repair would exceed it. It does not define requirements or review code.
- **Implementer** works only within the Frozen Spec, opens a PR, reports actual checks honestly, and repairs the existing PR after a formal request. It cannot approve or merge its own work or clear terminal labels.
- **Reviewer** evaluates only the Frozen Spec and the current PR head. It submits a native GitHub `APPROVE` or `REQUEST_CHANGES` review and enables native Auto-merge on the successful path. It never direct-merges or expands scope.
- **GitHub** owns PR state, checks, formal reviews, branch protection, and the merge performed by native Auto-merge.

## Issue coordination states

The five coordination labels are `agent-ready`, `agent-working`, `changes-requested`, `infra-blocked`, and `needs-human`. An executing Issue has at most one of these states. `frozen-spec` is an orthogonal classification label, not a coordination state. Do not create labels such as `pr-open`, `review-approved`, `merge-ready`, or `merged`; use GitHub's native PR, review, check, and merge state.

Initial work moves `agent-ready` → `agent-working`. A formal Reviewer rejection moves the Issue to `changes-requested`; reclaim moves it to `agent-working`. `infra-blocked` and `needs-human` are terminal stop states. Agents must not remove them; only a human maintainer can resolve the blocker and restart work. When an Issue closes, remove active labels (`agent-ready`, `agent-working`, `changes-requested`) and preserve any terminal label as evidence.

## Authority, repair budget, and deployment ownership

GitHub is authoritative for the Frozen Issue specification, coordination labels, PR identity/state/head SHA, checks, formal reviews, and merge state. Dispatcher-local durable state is authoritative only for runtime ownership, leases, attempts, repair admission/accounting, idempotency, launch receipts, reconciliation, and watchdog state; it cannot override contradictory current GitHub facts. Missing, corrupt, or untrusted local state never resets ownership, attempts, or repair budget. If consistency cannot be established, execution fails closed.

One logical Issue/PR flow has `MAX_AUTOMATED_REPAIRS = 3`, shared by implementation-attributable CI repairs and Reviewer-caused repairs. Initial implementation is not a repair. Reviewer evaluates only the Frozen Spec and exact current head, submits `APPROVE` or `REQUEST_CHANGES`, and keeps the existing successful native Auto-merge path. Reviewer neither counts formal rejections nor decides whether another repair is permitted. A red CI result alone is not a product rejection.

Dispatcher admits a candidate only when the workflow is valid, its cause is recognized, the exact current SHA matches the cause, that logical head has not already consumed a repair, budget remains, and no terminal/cancellation blocker exists. Logical repair identity is `repair:<repo>:pr:<pr_number>:head:<failed_or_rejected_head_sha>`. Replayed observations for a head cannot create duplicate logical repairs. Repairs continue on the same linked PR and implementation branch. Stale CI failures and reviews cannot authorize work against a newer SHA. Exceeding the budget transitions to `needs-human`.

Only positively attributed implementation failures within the Frozen Spec may be candidates for automatic CI repair. Confirmed infrastructure failure is `infra-blocked`; ambiguous or insufficient evidence is `needs-human`. A generic GitHub `failure` conclusion does not establish implementation causation. Detailed classification belongs to later runtime work.

Each consumer has one authorized production Dispatcher owner. This is a deployment invariant, not a guarantee of distributed election, cross-machine locking, or consensus. Machine-local SQLite can provide only local exclusion. Ownership ambiguity fails closed.

## Lifecycle and exact SHA

1. A human freezes scope and acceptance criteria in an Issue, adds `frozen-spec` and `agent-ready`.
2. Dispatcher/Sidecar claims it and marks `agent-working`.
3. Implementer opens a PR linked to exactly one Frozen Issue. The PR author must be allow-listed by that consumer's manifest.
4. The consumer's configured required checks run against the PR head. These are consumer-defined; this contract defines no application check names or commands.
5. A per-repository ChatGPT Work Reviewer webhook task runs for a GitHub event. On every run it reconstructs state from GitHub, identifies the one current head SHA, and makes at most one decision. A run must not rely on a prior run's memory.
6. In that same event-triggered run, Reviewer waits for every configured required check on the exact current head SHA. A check not yet observed (including one GitHub has not created yet) and an observed `queued`, `in_progress`, or `pending` check are both not-yet-terminal; absence on the first read is not an immediate missing-check failure. After every wait and re-read, verify the PR still has the captured head SHA. If it changes, stop the old run without a formal decision; `synchronize` starts the run for the new SHA. A terminal conclusion outside `accepted_conclusions` stops the approval path. If a check remains absent until the run's reasonable wait deadline or platform execution limit, report `missing/timeout` and exit without approval or inventing a CI result. There is no separate polling service, scheduled polling, Reviewer Wake, marker, comment wake, or check-run Task trigger.
7. If Frozen Spec criteria fail, Reviewer submits native `REQUEST_CHANGES` bound to the exact current head SHA and sets the Issue state to `changes-requested`. On the successful path, Reviewer enables native Auto-merge with the configured method, then submits `APPROVE` bound to the same exact SHA. If enabling Auto-merge fails, it does not approve.
8. GitHub native Auto-merge merges once repository branch protection, approval, and required checks are satisfied.

CI passing is necessary when configured, but never implies Reviewer approval. Old checks or reviews for a prior SHA cannot authorize the current head.

### Reviewer preconditions

Before formal review, verify all of the following from current GitHub state:

- PR is open and not Draft.
- Base branch equals the consumer manifest's configured branch.
- PR author is in the configured Implementer allow-list.
- Exactly one linked Frozen Issue can be determined.
- That Issue has `frozen-spec` and has neither `infra-blocked` nor `needs-human`.
- Current head SHA is unambiguous and still current at decision submission.
- Every configured required check has a successful result for that exact head SHA. The consumer defines accepted success conclusions in its manifest.
- The formal review is submitted against the exact current head SHA.

Wait within the triggering Reviewer run while exact-head checks are not yet observed or remain nonterminal. Report missing only if a check is still absent at the run's waiting deadline/platform limit. Recheck the PR head SHA after each wait. Do not use a separate polling service or rely on a future check-completion event to wake a Task. Reviewer events are `pull_request.opened`, `pull_request.ready_for_review`, and `pull_request.synchronize`; commit updates can be opt-in through the webhook configuration. Whether a consumer's Dispatcher/Sidecar is strictly event-driven must be established independently; this contract does not claim that property is proven.

## Same-PR repair

The implementer must update the same PR and linked Issue; opening a replacement PR does not satisfy repair. A push changes the head SHA, invalidating prior checks and review conclusions for authorization purposes. Required checks and review must apply to the new current head.

## Manifest and runtime boundaries

New desired manifests use `schema_version: 2` and `issue_contract.max_automated_repairs: 3`. The value is the maximum number of automated repair attempts admitted by Dispatcher across implementation-attributable CI failures and Reviewer `REQUEST_CHANGES`.

Node tooling owns PLAN, APPLY, AUDIT, manifest generation/validation, and Reviewer contract installation. Python generic runtime owns claim/ownership, dispatch, repair admission, durable execution state, reconciliation, and watchdog. They share the consumer manifest and this contract; Node tooling must not grow a runtime state machine, and Python runtime must not duplicate onboarding.

Current Node tooling recognizes schema v1 as legacy and returns a structured upgrade-required result; it need not reproduce every historical v1 compliance rule or silently rewrite an existing consumer. Existing pinned-revision safeguards remain in force. Future generic runtime execution supports v2 only: schema v1 is unsupported and must not be interpreted under either repair model. No general automatic v1-to-v2 migration engine is required.

## CI repair classification

Confirmed implementation failure may be eligible for automated repair. Confirmed infrastructure failure maps to `infra-blocked`. Ambiguous or insufficient evidence maps to `needs-human`. A generic failure conclusion alone is insufficient evidence of implementation causation; detailed classification is a later runtime concern.

## Merge and prohibited mechanisms

Use GitHub native `APPROVE`, `REQUEST_CHANGES`, branch protection, and Auto-merge. Reviewer enables native Auto-merge after confirming Frozen Spec and checks, and before approving. Reviewer never direct-merges. Normal flow forbids admin bypass. Do not use comment text as authorization, marker comments as wake signals, a Reviewer Wake job, a separate polling service, a custom merge controller, paid inference/API dependencies, reverse proxies, cookies, browser-session workarounds, or `gh-aw` as a v1 dependency.

## Consumer-owned configuration

Every consumer specifies its own base branch, allowed Implementer identities, required check names and accepted conclusions, and native merge method. Do not copy another repository's application checks by default. The manifest template and onboarding guide define the fields.
