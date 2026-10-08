# Consumer Onboarding

This guide connects one GitHub repository to the shared Control Plane contract. Every consumer owns its application CI and its own Reviewer Task instance. Run the CLI from a local clone of VCCP and the target consumer checkout; it does not copy the VCCP source tree into the consumer. Newly generated consumer manifests use schema v2 and one shared three-repair budget.

## 1. Configure the consumer

Create a fresh onboarding JSON input containing VCCP's canonical source and full commit SHA, the consumer base branch and Implementer authors, this repo's real required checks and accepted conclusions, and the chosen merge method. Required checks are consumer-defined and cannot be empty or placeholders. See `schemas/onboarding-input.schema.json`.

Example input:

```json
{
  "vccp": {
    "source": "liuchangchxy/vibe-coding-control-plane",
    "revision": "<the full 40-character VCCP commit SHA>"
  },
  "repository": {
    "base_branch": "main",
    "implementer_authors": ["my-builder[bot]"]
  },
  "reviewer": {
    "required_checks": [
      {
        "name": "Unit Tests",
        "accepted_conclusions": ["success"],
        "implementation_failure_conclusions": ["failure"],
        "infrastructure_failure_conclusions": ["timed_out", "startup_failure"]
      }
    ]
  },
  "merge": { "method": "squash" },
  "reporting": { "pull_request_template": false }
}
```

First run PLAN and inspect `current`, `desired`, diffs, warnings, and external steps. PLAN does not mutate local files or GitHub. Save its JSON output. APPLY accepts that saved PLAN only and rejects it if the checkout or observed GitHub/file state changed. Run AUDIT at any time to observe compliance and drift. The consumer manifest is consumer-owned; VCCP will not replace it except an explicitly proposed revision update that patches only `vccp.revision`. Existing schema v1 manifests are parsed as legacy and return `repository_upgrade_required`; PLAN/APPLY/AUDIT do not silently rewrite them. Current tooling does not promise to reproduce every historical v1 compliance check, and v1 is unsupported by the future generic runtime.

APPLY creates only missing standard label identities. Existing labels are never renamed, edited, or deleted; differing colors/descriptions are audit warnings. Treat `frozen-spec` as a separate classification label. An executing Issue may have only one coordination label.

## 2. Install completion cleanup

APPLY installs a thin workflow adapter referencing VCCP's central cleanup Action at the exact full source commit SHA in the manifest. The adapter needs `issues: write`; the Action removes active labels and preserves terminal labels. Its implementation remains central and is versioned with VCCP.

The optional `.github/PULL_REQUEST_TEMPLATE/vccp.md` is only a reporting aid. If the consumer already has a default PR template, onboarding leaves it untouched and does not force a second template. The Reviewer contract itself enforces exactly one linked Frozen Issue and current PR state.

## 3. Create a per-repository Reviewer task

In ChatGPT Work, create a GitHub webhook Task specifically for this consumer repository and attach the rendered `.github/vccp/reviewer-task-prompt.md`. This is an external step: local CLI cannot create or verify the Task or claim it exists. The Task starts on `opened`, `ready_for_review`, and `synchronize`; within the same event-triggered run it treats both an unobserved check (which may not have been created yet) and a queued/in-progress/pending check as not-yet-terminal and continues waiting. It rechecks the captured head SHA after every wait, and only reports a still-absent check as `missing/timeout` at the run deadline/platform limit. A terminal unaccepted conclusion stops approval. No check-completion trigger or separate polling mechanism is used.

Dispatcher registration is also a pending external step; this onboarding tool does not implement a Dispatcher. Branch protection and rulesets are read-only audited and remain an external maintainer step. Onboarding never replaces these objects.

## 4. Operational boundary

For example, from a clean VCCP checkout, obtain its source revision with `git rev-parse HEAD` and place that exact SHA into `onboarding.json`. Then run `node scripts/onboard-consumer.mjs plan --repo OWNER/REPO --path /path/to/consumer --input onboarding.json > /tmp/vccp-plan.json`, inspect the file, and run `node scripts/onboard-consumer.mjs apply --repo OWNER/REPO --path /path/to/consumer --plan /tmp/vccp-plan.json`. `node scripts/audit-consumer.mjs --repo OWNER/REPO --path /path/to/consumer` emits a machine-readable audit report. The CLI verifies its runtime checkout matches the desired VCCP revision so generated files and central Action references cannot claim a different source revision. No command commits, pushes, creates a PR, or modifies a consumer outside the selected checkout.

Machine-readable overall states and process exit codes are stable: `repository_changes_planned` (0), `repository_ready_external_pending` (0), `repository_upgrade_required` (0), `repository_blocked` (2), `command_failed` (3), and `stale_plan` (4). `repository_upgrade_required` identifies a recognized legacy schema v1 manifest and does not authorize APPLY to rewrite it. A zero from PLAN means planning completed successfully; inspect `overall_status` and `items` to see whether APPLY changes remain. `repository_ready_external_pending` means repository-side state is compliant while Reviewer Task, Dispatcher, and/or protection verification still needs an external actor. It never means operationally ready.

For future new projects, `vibe-coding-starter` calls this same CLI/API after creating the repository. It must not implement a second onboarding path.

`vibe-coding-control-plane` is the canonical source for these automation contracts and templates. `vibe-coding-starter` is a consumer/bootstrapper that may automate future onboarding. Existing projects such as EasyExam, DaySpark, and Zhanghui can migrate through this guide. Control Plane does not depend on the starter.

Node tooling owns PLAN, APPLY, AUDIT, manifest generation/validation, and Reviewer contract installation. The Python runtime owns claim/ownership, dispatch, repair admission, durable execution state, reconciliation, watchdog, and one-shot exact-head CI / Review / native-merge observation. Both use the consumer manifest and VCCP contract; neither duplicates the other's responsibilities.
