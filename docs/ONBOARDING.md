# Consumer Onboarding

This guide covers two separate concerns: repository onboarding installs the shared Control Plane contract, while machine-local activation supplies paths, credentials, and ownership needed to construct the Generic Runtime. Every consumer owns its application CI and its own Reviewer Task instance. Run the CLI from a local clone of VCCP and the target consumer checkout; it does not copy the VCCP source tree into the consumer. Newly generated consumer manifests use schema v2 and one shared three-repair budget.

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

PLAN reports capabilities independently of filenames. For `coordination-label-cleanup`, it accepts a consumer-owned `issues.closed` handler when its inspected behavior removes active coordination labels on every close, preserves terminal labels as evidence, and does not add labels or otherwise reactivate work. A missing capability plans the VCCP-managed adapter; an unproven or conflicting behavior blocks PLAN/AUDIT. APPLY re-plans before writing. The result records `satisfied`, `missing`, or `conflict` and the provider (`consumer` or `vccp-managed`).

APPLY creates only missing standard label identities. Existing labels are never renamed, edited, or deleted; differing colors/descriptions are audit warnings. Treat `frozen-spec` as a separate classification label. An executing Issue may have only one coordination label.

## 2. Install completion cleanup

When cleanup is missing, APPLY installs a thin workflow adapter referencing VCCP's central cleanup Action at the exact full source commit SHA in the manifest. The adapter needs `issues: write`; the Action removes active labels and preserves terminal labels. If a consumer implementation already satisfies the behavior, no VCCP cleanup artifact is planned.

The optional `.github/PULL_REQUEST_TEMPLATE/vccp.md` is only a reporting aid. If the consumer already has a default PR template, onboarding leaves it untouched and does not force a second template. The Reviewer contract itself enforces exactly one linked Frozen Issue and current PR state.

## 3. Create a per-repository Reviewer task

In ChatGPT Work, create a GitHub webhook Task specifically for this consumer repository and attach the rendered `.github/vccp/reviewer-task-prompt.md`. This is an external step: local CLI cannot create or verify the Task or claim it exists. The Task starts on `opened`, `ready_for_review`, and `synchronize`; within the same event-triggered run it treats both an unobserved check (which may not have been created yet) and a queued/in-progress/pending check as not-yet-terminal and continues waiting. It rechecks the captured head SHA after every wait, and only reports a still-absent check as `missing/timeout` at the run deadline/platform limit. A terminal unaccepted conclusion stops approval. No check-completion trigger or separate polling mechanism is used.

Branch protection and rulesets are read-only audited and remain an external maintainer step. Onboarding never replaces these objects. The existing Python Generic Runtime is the runtime wiring boundary; no future Dispatcher registration is required by repository onboarding.

## 4. Operational boundary

For example, from a clean VCCP checkout, obtain its source revision with `git rev-parse HEAD` and place that exact SHA into `onboarding.json`. Then run `node scripts/onboard-consumer.mjs plan --repo OWNER/REPO --path /path/to/consumer --input onboarding.json > /tmp/vccp-plan.json`, inspect the file, and run `node scripts/onboard-consumer.mjs apply --repo OWNER/REPO --path /path/to/consumer --plan /tmp/vccp-plan.json`. `node scripts/onboard-consumer.mjs audit --repo OWNER/REPO --path /path/to/consumer` emits a repository audit. The CLI verifies its runtime checkout matches the desired VCCP revision so generated files and central Action references cannot claim a different source revision. No command commits, pushes, creates a PR, or modifies a consumer outside the selected checkout.

## 5. Machine-local runtime activation

Repository onboarding writes `.github/control-plane.yml`, the shared consumer contract. Machine-local activation is separate: a JSON file on the runtime host supplies local paths, the SQLite location, owner identity, and GitHub App ID plus a path to a current-user DPAPI-protected private key. Never put these values or credential material in `.github/control-plane.yml`, installation lock, generated onboarding files, or another repository-managed artifact. Installation tokens are short-lived and held only in process memory; the runtime database stores no credentials.

Example `C:/Users/<user>/.vccp/consumer-runtime.json` (replace every example path locally). This file stays on the machine and is the single canonical repository identity for this runtime:

```json
{
  "database_path": "C:/Users/<user>/.vccp/state/consumer.sqlite",
  "workspace": "C:/work/consumer",
  "target_repository": "OWNER/REPO",
  "enrollment": { "authorized_repository": "OWNER/REPO" },
  "owner_id": "local-runtime-owner",
  "antigravity_executable": "C:/Users/<user>/AppData/Local/AntiGravity/language_server.exe",
  "github_app": {
    "app_id": "123456",
    "expected_app_slug": "consumer-implementer",
    "credential_source": {
      "type": "windows_dpapi_file",
      "path": "C:/Users/<user>/.vccp/credentials/github-app-key.dpapi"
    }
  },
  "launch_timeout_seconds": 60,
  "recovery_timeout_seconds": 900,
  "implementer_progress_timeout_seconds": 1800,
  "lifecycle_timeouts": {
    "waiting_ci": 3600,
    "waiting_review": 86400,
    "waiting_merge": 86400
  }
}
```

The database parent directory must already exist and be writable. `target_repository`, the enrollment authorization, the CLI `--repo`, the consumer manifest path and workspace `origin` must identify the same repository. The enrollment marker is an explicit human action; PLAN/AUDIT readiness never creates it. The DPAPI key file is created by a human-controlled one-time bootstrap, for example `python -m vccp_runtime.github_app C:/secure-source/app-private-key.pem C:/Users/<user>/.vccp/credentials/github-app-key.dpapi`. This command reads the PEM into memory, encrypts it for the current Windows user, and creates the destination without overwriting an existing file. Creating/selecting the App, installing it, approving permissions, importing the private key, and authorizing enrollment remain human actions.

The runtime probes installation and granted permissions without changing them. It requests only `contents:write`, `issues:write`, `pull_requests:write`, `checks:read`, and `metadata:read`; `metadata:read` is the GitHub App baseline permission. `contents:write` supports controlled pushes, `issues:write` supports coordination labels, `pull_requests:write` supports linked PR creation, and `checks:read` supports CI observation. Actions permissions are not needed in this package. If installation or permissions are missing, readiness reports the human action required; VCCP never changes App installation or permissions.

Run qualification explicitly:

```sh
node scripts/onboard-consumer.mjs readiness --repo OWNER/REPO --path /path/to/consumer --runtime-config /path/to/consumer-runtime.json
```

Readiness validates the schema-v2 repository contract, the matching repository identities, explicit machine enrollment, workspace and SQLite locations, executable and configured conversation roots, reads the protected private key, verifies App installation and permissions, mints a short-lived token, performs a harmless repository read, constructs the production writer/push/runtime graph using temporary SQLite, and tries then releases the machine process lock. It does not dispatch Issues, mutate GitHub labels, create PRs, push, start repairs, or launch AntiGravity conversations. Installation/token/read calls are read-only and never report credential material.

Repository contract, machine authorization, and machine activation are reported separately. PLAN/AUDIT without a runtime config leave authorization and activation `unknown`; a supplied config lets AUDIT qualify both. Readiness reports `machine_activation_ready` only when enrollment, App installation, minimum permissions, harmless read, local prerequisites, lock acquisition and production graph construction succeed. Missing human authorization or prerequisites returns `machine_activation_blocked` with blocker reasons and exit code 2. This status does not imply an always-running daemon or completed business operation.

Machine-local Sidecar launchers can use `sidecar.json` to invoke `python -m vccp_runtime.runner --manifest <local-manifest-path> --runtime-config <local-config-path> --repo OWNER/REPO --continuous --poll-interval 30`. Keep both paths machine-local; do not commit machine-specific absolute paths to a consumer. The runner verifies enrollment, target identity and workspace origin before constructing the production runtime. Starting it performs normal production cycles, startup recovery, lifecycle/watchdog advancement and `agent-ready` discovery. Readiness never starts the runner or dispatches work. Do not enable a resident launcher until machine activation is ready and enrollment is authorized.

Machine-readable repository overall states and process exit codes are: `repository_changes_planned` (0), `repository_ready` (0), `repository_upgrade_required` (0), `repository_blocked` (2), `command_failed` (3), and `stale_plan` (4). `repository_upgrade_required` identifies a recognized legacy schema v1 manifest and does not authorize APPLY to rewrite it. A zero from PLAN means planning completed successfully; inspect `overall_status` and `items` to see whether APPLY changes remain. Repository readiness does not imply machine-local runtime activation; read `runtime_activation.status` or run the readiness command.

For future new projects, `vibe-coding-starter` calls this same CLI/API after creating the repository. It must not implement a second onboarding path.

`vibe-coding-control-plane` is the canonical source for these automation contracts and templates. `vibe-coding-starter` is a consumer/bootstrapper that may automate future onboarding. Existing projects such as EasyExam, DaySpark, and Zhanghui can migrate through this guide. Control Plane does not depend on the starter.

Node tooling owns PLAN, APPLY, AUDIT, manifest generation/validation, and Reviewer contract installation. Python owns activation qualification and construction of the Generic Runtime, plus claim/ownership, discovery, dispatch, repair admission, durable execution state, reconciliation, Implementer and lifecycle watchdogs, and exact-head CI / Review / native-merge observation. The shared manifest holds consumer policy; local JSON holds machine configuration. These boundaries prevent host-specific paths and credential data from becoming shared repository contract.
