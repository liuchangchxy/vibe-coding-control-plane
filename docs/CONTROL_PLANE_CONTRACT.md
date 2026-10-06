# Control Plane Contract v0

This document defines the cross-repository automation contract. GitHub Issues, Pull Requests, labels, check runs, reviews, and merge state are the durable source of truth. Chat transcripts and a task's prior runs are never authoritative.

## Authority

- **Human** freezes the Issue specification, resolves terminal stops, and owns decisions beyond the frozen scope.
- **Dispatcher/Sidecar** claims eligible Issues and routes initial or repair work. It does not define requirements or review code.
- **Implementer** works only within the Frozen Spec, opens a PR, reports actual checks honestly, and repairs the existing PR after a formal request. It cannot approve or merge its own work or clear terminal labels.
- **Reviewer** evaluates only the Frozen Spec and the current PR head. It submits a native GitHub `APPROVE` or `REQUEST_CHANGES` review. It does not expand scope or merge.
- **GitHub** owns PR state, checks, formal reviews, branch protection, and the merge performed by native Auto-merge.

## Issue coordination states

The five coordination labels are `agent-ready`, `agent-working`, `changes-requested`, `infra-blocked`, and `needs-human`. An executing Issue has at most one of these states. `frozen-spec` is an orthogonal classification label, not a coordination state. Do not create labels such as `pr-open`, `review-approved`, `merge-ready`, or `merged`; use GitHub's native PR, review, check, and merge state.

Initial work moves `agent-ready` → `agent-working`. A formal Reviewer rejection moves the Issue to `changes-requested`; reclaim moves it to `agent-working`. `infra-blocked` and `needs-human` are terminal stop states. Agents must not remove them; only a human maintainer can resolve the blocker and restart work. When an Issue closes, remove active labels (`agent-ready`, `agent-working`, `changes-requested`) and preserve any terminal label as evidence.

## Lifecycle and exact SHA

1. A human freezes scope and acceptance criteria in an Issue, adds `frozen-spec` and `agent-ready`.
2. Dispatcher/Sidecar claims it and marks `agent-working`.
3. Implementer opens a PR linked to exactly one Frozen Issue. The PR author must be allow-listed by that consumer's manifest.
4. The consumer's configured required checks run against the PR head. These are consumer-defined; this contract defines no application check names or commands.
5. A per-repository ChatGPT Work Reviewer webhook task runs for a GitHub event. On every run it reconstructs state from GitHub, identifies the one current head SHA, and makes at most one decision. A run must not rely on a prior run's memory.
6. Reviewer returns without a formal review if any prerequisite is missing, any configured check is pending/failed, or the head changed during evaluation. A later GitHub event can start a fresh run. There is no Reviewer polling loop.
7. If the Frozen Spec is met, Reviewer submits native `APPROVE` bound to that exact current head SHA. If it is not met, Reviewer submits native `REQUEST_CHANGES` bound to that SHA and sets the Issue state to `changes-requested`.
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

If check status is pending or unavailable, exit without review; do not wait by polling. Native event delivery must start another run when relevant state changes. Whether a consumer's Dispatcher/Sidecar is strictly event-driven must be established independently; this contract does not claim that property is proven.

## Same-PR repair and round limit

Every formal `REQUEST_CHANGES` counts as one repair round. The implementer must update the same PR and linked Issue; opening a replacement PR does not satisfy repair. A push changes the head SHA, invalidating prior checks and review conclusions for authorization purposes. Required checks and review must apply to the new current head.

After the third formal `REQUEST_CHANGES`, set `needs-human` and stop. Never automatically begin a fourth repair round. A human must resolve the stop before work restarts.

## Merge and prohibited mechanisms

Use GitHub native `APPROVE`, `REQUEST_CHANGES`, branch protection, and Auto-merge. Normal Implementer flow forbids direct merge and admin bypass. Do not use comment text as authorization, marker comments as wake signals, a Reviewer Wake job, a polling Reviewer, a custom merge controller, paid inference/API dependencies, reverse proxies, cookies, browser-session workarounds, or `gh-aw` as a v1 dependency.

## Consumer-owned configuration

Every consumer specifies its own base branch, allowed Implementer identities, required check names and accepted conclusions, and native merge method. Do not copy another repository's application checks by default. The manifest template and onboarding guide define the fields.
