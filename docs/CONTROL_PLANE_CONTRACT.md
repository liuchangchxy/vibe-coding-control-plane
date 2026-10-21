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

Canonical PR adoption restores PR identity trust only after current GitHub relationship, author, base, and authorization checks. Repair-budget provenance remains separate: a surviving durable count is preserved across adoption, while a missing count is marked unknown. An adopted PR with unknown repair history remains observable for CI, review, and merge, but any requested automated repair transitions to `needs-human` with `repair_history_or_budget_provenance_unknown`.

One logical Issue/PR flow has independent caps of three implementation-attributable CI repairs and three formal Reviewer repairs, plus a hard total of five automated repairs. Initial implementation is not a repair. Three per category allows the observed multi-round CI and Reviewer fixes; the lower total cap bounds cross-category execution. Same-head causes deduplicate, and exhausting either cap stops dispatch. A red CI result alone is not a product rejection. Reviewer evaluates only the Frozen Spec and exact current head, submits `APPROVE` or `REQUEST_CHANGES`, and keeps the existing successful native Auto-merge path. Reviewer neither counts formal rejections nor decides whether another repair is permitted.

An operator may record one-repair continuations after `BUDGET_EXHAUSTED`; there is no arbitrary lifetime count cap. Automatic budgets remain three CI repairs, three Reviewer repairs, and five automatic repairs total. Each grant is durable and bound to one exhausted attempt, repository, Issue, PR, exact head, cause type, and cause ID; it adds exactly one separately counted authorized repair and never raises an automatic budget ceiling. Admission consumes the grant and creates its Repair Attempt atomically. The grant does not reset repair counts or delete attempts. A closed PR, changed head, changed/stale Formal Review or current CI evidence, ambiguous cause, or non-`needs-human` stop rejects authorization. `--authorized-by` is an operator-supplied audit identity; repository enrollment and GitHub App permissions constrain execution, but the runtime does not independently authenticate the named person as a maintainer.

The production runner exposes the explicit operator entry point:

```powershell
python -m vccp_runtime.runner --manifest <manifest> --runtime-config <runtime-config> --repo OWNER/REPO --authorize-repair-continuation --issue ISSUE --pr PR --head-sha FULL_40_CHAR_SHA --authorized-by MAINTAINER_ID
```

If GitHub label restoration is interrupted, rerun the same command only while its exact HEAD and cause evidence remain current; VCCP resumes the pending grant without adding another grant or attempt.

It records the authorization and restores the Issue's matching active coordination label. It can run while the resident Runner is active because it makes no dispatch and relies on SQLite transactions plus GitHub compare-and-set. The normal Runner then dispatches the exact cause; the authorization command itself never launches an Implementer.

Dispatcher admits a candidate only when the workflow is valid, its cause is recognized, the exact current SHA matches the cause, that logical head has not already consumed a repair, budget remains, and no terminal/cancellation blocker exists. Logical repair identity is `repair:<repo>:pr:<pr_number>:head:<failed_or_rejected_head_sha>`. Replayed observations for a head cannot create duplicate logical repairs. Repairs continue on the same linked PR and implementation branch. Stale CI failures and reviews cannot authorize work against a newer SHA. Exceeding the budget transitions to `needs-human`.

Only positively attributed implementation failures within the Frozen Spec may be candidates for automatic CI repair. Confirmed infrastructure failure is `infra-blocked`; ambiguous or insufficient evidence is `needs-human`. A generic GitHub `failure` conclusion does not establish implementation causation. Schema-v2 consumers may explicitly list `implementation_failure_conclusions` and `infrastructure_failure_conclusions` per required check; omitted or conflicting attribution fails closed.

Each consumer has one authorized production Dispatcher owner. This is a deployment invariant, not a guarantee of distributed election, cross-machine locking, or consensus. Machine-local SQLite can provide only local exclusion. Ownership ambiguity fails closed.

## Lifecycle and exact SHA

1. A human freezes scope and acceptance criteria in an Issue, adds `frozen-spec` and `agent-ready`.
2. Dispatcher/Sidecar claims it and marks `agent-working`.
3. Implementer opens a PR linked to exactly one Frozen Issue. The PR author must be allow-listed by that consumer's manifest.
4. The consumer's configured required checks run against the PR head. These are consumer-defined; this contract defines no application check names or commands.
5. The per-repository Local Reviewer runs through VCCP Runtime on one exact PR head. The host verifies GitHub facts and performs formal review writes; the Claude Agent SDK agent inspects a read-only workspace and returns a structured verdict.
6. Runtime observes each required check for the captured exact head SHA before requesting a Reviewer decision. An absent, queued, in-progress, or pending check is not yet terminal. The head must be rechecked after each observation; stale heads receive no decision. A terminal unaccepted check stops approval, and a check that never appears cannot be called successful.
7. If Frozen Spec criteria fail, Reviewer submits native `REQUEST_CHANGES` bound to the exact current head SHA and sets the Issue state to `changes-requested`. On the successful path, Reviewer enables native Auto-merge with the configured method, then submits `APPROVE` bound to the same exact SHA. If enabling Auto-merge fails, it does not approve.
8. GitHub native Auto-merge merges once repository branch protection, approval, and required checks are satisfied.

CI passing is necessary when configured, but never implies Reviewer approval. Old checks or reviews for a prior SHA cannot authorize the current head.

### Reviewer preconditions

Before formal review, verify all of the following from current GitHub state:

- PR is open and not Draft.
- Base branch equals the consumer manifest's configured branch.
- PR author is in the configured Implementer allow-list.
- For a normal development PR, exactly one linked Frozen Issue can be determined and it has `frozen-spec` with neither `infra-blocked` nor `needs-human`.
- A VCCP deploy revision-upgrade PR may use the maintenance review path instead of an Issue only when its deploy metadata, exact managed-file diff, manifest/lock revisions, artifact hashes, and canonical target commit all verify. This path does not apply to removal or other infrastructure PRs.
- Current head SHA is unambiguous and still current at decision submission.
- Every configured required check has a successful result for that exact head SHA. The consumer defines accepted success conclusions in its manifest.
- The formal review is submitted against the exact current head SHA.

The Runtime observes exact-head checks before Local Reviewer execution. Stale heads receive no approval. The local Reviewer operates on a disposable exact-head workspace; VCCP host alone submits GitHub reviews and enables native Auto-merge.

## Same-PR repair

The implementer must update the same PR and linked Issue; opening a replacement PR does not satisfy repair. A push changes the head SHA, invalidating prior checks and review conclusions for authorization purposes. Required checks and review must apply to the new current head.

## Manifest and runtime boundaries

New desired manifests continue using `schema_version: 2` and `issue_contract.max_automated_repairs: 3` for deployed-consumer compatibility. The runtime applies the classified limits above; the existing schema-v2 field is retained during this compatibility transition.

Node tooling owns PLAN, APPLY, AUDIT, manifest generation/validation, and Reviewer contract installation. Python Generic Runtime owns machine-local activation qualification and runtime construction, claim/ownership, automatic ready discovery, active recovery discovery, a single-machine production runner, durable execution state, restart reconciliation, Implementer progress observation, watchdogs, and exact-head CI / Review / native-merge observation. The shared consumer manifest defines cross-machine policy; machine-local configuration supplies paths, SQLite location, owner identity, executable locations, protected GitHub App credential source, artifact roots, and local timeouts. Do not place those machine-specific values or credential values in the shared manifest or generated repository artifacts. Readiness verifies App installation and minimum permissions and performs a harmless repository read, but does not dispatch or mutate GitHub state. Node tooling must not grow a runtime state machine, and Python runtime must not duplicate repository onboarding.

PLAN, APPLY, and AUDIT recognize schema v1 as legacy and return a structured upgrade-required result; they do not rewrite it. `deploy` converts a recognized v1 manifest to v2 and includes the generated v2 lock and managed artifacts in one reviewable maintenance PR. Existing pinned-revision safeguards remain in force. Generic runtime execution supports v2 only: schema v1 is unsupported and must not be interpreted under either repair model.

## CI repair classification

For each required check, the lifecycle driver uses the current PR head SHA only. A terminal conclusion listed under `accepted_conclusions` is accepted. A nonaccepted conclusion listed under `implementation_failure_conclusions` can create an exact-head CI repair candidate through `RuntimeCore.dispatch_repair()`. A conclusion listed under `infrastructure_failure_conclusions` remains in bounded `WAITING_CI` re-observation for two retries, then maps to `infra-blocked` if still present. Unlisted, conflicting, or insufficient attribution maps to `needs-human`; `failure` has no implicit implementation meaning. The two classification lists are optional for schema-v2 compatibility.

## Merge and prohibited mechanisms

Use GitHub native `APPROVE`, `REQUEST_CHANGES`, branch protection, and Auto-merge. Reviewer enables native Auto-merge after confirming Frozen Spec and checks, and before approving. Reviewer never direct-merges. Normal flow forbids admin bypass. Do not use comment text as authorization, marker comments as wake signals, a Reviewer Wake job, a separate polling service, a custom merge controller, paid inference/API dependencies, reverse proxies, cookies, browser-session workarounds, or `gh-aw` as a v1 dependency.

## Consumer-owned configuration

Every consumer specifies its own base branch, allowed Implementer identities, required check names and accepted conclusions, and native merge method. Do not copy another repository's application checks by default. The manifest template and onboarding guide define the fields.

## Evidence and scope decisions

Use evidence in this order: current GitHub/source/runtime evidence; the formal contract and current documentation; current machine observation; historical reports; chat memory; AI inference. Chat history is not authority. A Worker must verify Controller-provided claims against current evidence instead of promoting a prompt's guess into a system fact.

Before calling a problem a VCCP architecture blocker, the Controller records concise answers to: (1) what outcome the user needs; (2) which existing VCCP capability was checked; (3) whether the blocker is observed at runtime or inferred; (4) whether current GitHub, Runtime, or OS capabilities can meet the goal; and (5) why new infrastructure is necessary. If an existing capability meets the goal, do not expand scope for architectural preference.

Consumer onboarding and revision-upgrade Workers use a branch and PR. They do not push directly to `main` without explicit authorization. Infrastructure maintenance PRs are not product PRs: use an independent human review where available, do not invent a Frozen Issue or enable self-review, and require explicit maintainer authorization for any exceptional administrator merge. Re-read the full diff, expected files, current head SHA, and exact-head checks before that operation; use the GitHub CLI head guard documented in [Consumer deployment and host migration](ONBOARDING.md#production-deployment-and-host-migration).

Reports read every SHA from current GitHub or Git state. Name the PR head SHA and merge commit SHA separately; never infer either from memory or a previous report. Historical SHA values are context, not current revision evidence.

Classify a GitHub EOF from direct command output as an interrupted request/stream. Do not infer that VCCP must manage proxy, DNS, or TLS, and do not add retry infrastructure unless current source and repeated runtime evidence establish that need.
