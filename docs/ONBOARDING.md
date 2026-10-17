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

The database parent directory must already exist and be writable. `target_repository`, the enrollment authorization, the CLI `--repo`, the consumer manifest path and workspace `origin` must identify the same repository. The enrollment marker is an explicit human action; PLAN/AUDIT readiness never creates it. On Windows, the DPAPI key file is created by a human-controlled one-time bootstrap, for example `python -m vccp_runtime.github_app C:/secure-source/app-private-key.pem C:/Users/<user>/.vccp/credentials/github-app-key.dpapi`. This command reads the PEM into memory, encrypts it for the current Windows user, and creates the destination without overwriting an existing file. On POSIX/macOS hosts, configure `"type": "posix_file"` pointing to a PEM private key with strict `0600` permissions owned by the current user (creatable via `python -m vccp_runtime.github_app /secure-source/key.pem ~/.vccp/credentials/github-app-key.pem --type posix_file`). Creating/selecting the App, installing it, approving permissions, importing the private key, and authorizing enrollment remain human actions.

The runtime probes installation and granted permissions without changing them. It requests only `contents:write`, `issues:write`, `pull_requests:write`, `checks:read`, and `metadata:read`; `metadata:read` is the GitHub App baseline permission. `contents:write` supports controlled pushes, `issues:write` supports coordination labels, `pull_requests:write` supports linked PR creation, and `checks:read` supports CI observation. Actions permissions are not needed in this package. If installation or permissions are missing, readiness reports the human action required; VCCP never changes App installation or permissions.

Run qualification explicitly:

```sh
node scripts/onboard-consumer.mjs readiness --repo OWNER/REPO --path /path/to/consumer --runtime-config /path/to/consumer-runtime.json
```

For one read-only view of the repository audit, pinned revision, explicit machine provider bindings, readiness, and the native host runner, use:

```sh
node scripts/vccp.mjs status --repo OWNER/REPO --path /path/to/consumer --runtime-config /path/to/consumer-runtime.json
```

`status` reports `READY`, `NEEDS_ACTION`, or `BLOCKED`. A running-revision mismatch or missing provider binding remains visible in the component evidence and does not get repaired by this read-only command.

Readiness first runs the same canonical repository AUDIT used by the `audit` command, including capability inspection, cleanup conflict detection, and managed artifact drift checks. It then independently qualifies machine authorization and activation. The report preserves all three states; machine wiring can be technically ready while the overall result remains blocked if AUDIT reports repository changes, drift, or conflict. Readiness does not APPLY the plan.

Machine qualification validates explicit enrollment, matching repository identities, workspace and SQLite locations, executable and configured conversation roots, reads the protected private key, verifies App installation and permissions, mints a short-lived token, performs a harmless repository read, constructs the production writer/push/runtime graph using temporary SQLite, and tries then releases the machine process lock. It does not dispatch Issues, mutate GitHub labels, create PRs, push, start repairs, or launch AntiGravity conversations. Installation/token/read calls are read-only and never report credential material.

Repository contract, machine authorization, and machine activation are reported separately. PLAN/AUDIT without a runtime config leave authorization and activation `unknown`; a supplied config lets AUDIT qualify both. Readiness reports `machine_activation_ready` only when enrollment, App installation, minimum permissions, harmless read, local prerequisites, lock acquisition and production graph construction succeed. Missing human authorization or prerequisites returns `machine_activation_blocked` with blocker reasons and exit code 2. This status does not imply an always-running daemon or completed business operation.

Machine-local Sidecar launchers can use `sidecar.json` to invoke `python -m vccp_runtime.runner --manifest <local-manifest-path> --runtime-config <local-config-path> --repo OWNER/REPO --continuous --poll-interval 30`. Keep both paths machine-local; do not commit machine-specific absolute paths to a consumer. The runner verifies enrollment, target identity and workspace origin before constructing the production runtime. Starting it performs normal production cycles, startup recovery, lifecycle/watchdog advancement and `agent-ready` discovery. Readiness never starts the runner or dispatches work. Do not enable a resident launcher until machine activation is ready and enrollment is authorized.

Machine-readable repository overall states and process exit codes are: `repository_changes_planned` (0), `repository_ready` (0), `repository_upgrade_required` (0), `repository_blocked` (2), `command_failed` (3), and `stale_plan` (4). `repository_upgrade_required` identifies a recognized legacy schema v1 manifest and does not authorize APPLY to rewrite it. A zero from PLAN means planning completed successfully; inspect `overall_status` and `items` to see whether APPLY changes remain. Repository readiness does not imply machine-local runtime activation; read `runtime_activation.status` or run the readiness command.

For future new projects, `vibe-coding-starter` calls this same CLI/API after creating the repository. It must not implement a second onboarding path.

`vibe-coding-control-plane` is the canonical source for these automation contracts and templates. `vibe-coding-starter` is a consumer/bootstrapper that may automate future onboarding. Existing projects such as EasyExam, DaySpark, and Zhanghui can migrate through this guide. Control Plane does not depend on the starter.

Node tooling owns PLAN, APPLY, AUDIT, manifest generation/validation, and Reviewer contract installation. Python owns activation qualification and construction of the Generic Runtime, plus claim/ownership, discovery, dispatch, repair admission, durable execution state, reconciliation, Implementer and lifecycle watchdogs, and exact-head CI / Review / native-merge observation. The shared manifest holds consumer policy; local JSON holds machine configuration. These boundaries prevent host-specific paths and credential data from becoming shared repository contract.

## 6. Production deployment and host migration

### Readiness states are separate

Track these distinct readiness states in order; reaching one does not prove the next:

1. **Repository onboarded** — the schema v2 manifest, installation lock, cleanup capability, labels, PLAN/APPLY/AUDIT contract, and repository-level checks agree.
2. **Machine qualified** — `readiness` reports `machine_activation_ready` after repository identity, local config, credential availability, App installation and permissions, a harmless GitHub read, runtime graph construction, and local process-lock eligibility succeed. This is pre-start qualification. It does not mean a production Dispatcher is running.
3. **Dispatcher installed** — the host's native resident mechanism is configured (for example, a macOS LaunchAgent or an already validated Windows Sidecar mechanism). A local file alone does not prove it is loaded.
4. **Dispatcher running** — the process is alive, it acquired the local RuntimeLock before constructing the graph, its logs are accessible, and restart behavior has been observed. The continuous Runner retains that lock while it runs.
5. **Production verified** — a real product Issue labeled `frozen-spec` and `agent-ready` completes Claim → Implementer → PR → CI → Reviewer → repair if needed → GitHub native merge → Issue closeout. Only this proves full zero-touch product E2E for that host.

For evidence reporting, use the operational levels below: Level 1 repository ready, Level 2 machine ready, Level 3 Dispatcher running, Level 4 Issue discovery observed, and Level 5 full zero-touch E2E. Installation is a distinct prerequisite state between machine qualification and running.

### Consumer upgrade and start sequence

1. Read the Consumer's current default branch, manifest, installation lock, and pinned VCCP revision. Independently verify the external Reviewer Task state and whether a production Dispatcher owner already exists. A local prompt file does not prove a Reviewer Task exists or is enabled; if the execution context cannot inspect Work Tasks, report `Reviewer external status requires verification`.
2. Select a full 40-character VCCP commit SHA. Never pin a branch name.
3. From the VCCP checkout at that exact SHA, run `node scripts/onboard-consumer.mjs plan --repo OWNER/REPO --path /path/to/consumer --input onboarding.json`. Inspect the desired revision, file diffs, required checks, artifacts, and external steps.
4. Apply only the saved PLAN, using `node scripts/onboard-consumer.mjs apply --repo OWNER/REPO --path /path/to/consumer --plan /path/to/plan.json`. APPLY revalidates observed state and rejects a stale plan.
5. Commit the Consumer changes on a branch and open an upgrade PR. Do not push to `main` without explicit authorization. Merge the Consumer revision PR through its authorized review path; then confirm Consumer `main` contains the manifest and lock update.
6. Stop the production Runner. Check out/sync VCCP to the exact SHA now pinned by Consumer `main`. The production Runner compares its own VCCP checkout SHA with `vccp.revision` and refuses to start on a mismatch. Do not run a production manifest from an unmerged Consumer PR branch as authority.
7. With the resident Runner stopped, run `node scripts/onboard-consumer.mjs readiness --repo OWNER/REPO --path /path/to/consumer --runtime-config /absolute/path/to/consumer-runtime.json`. Resolve blockers, and only then start the resident host.

### Unified `vccp deploy`

Use `node scripts/vccp.mjs deploy --repo OWNER/REPO --path /path/to/consumer --runtime-config /absolute/path/to/consumer-runtime.json [--input onboarding.json]`. `deploy` reads canonical VCCP `main`, the Consumer checkout, its manifest/installation lock, machine config, exact runtime checkout, and host state, then delegates repository planning to PLAN and host qualification to readiness. First install and revision upgrade use the same path. Existing provider bindings are immutable. For a new binding, explicitly pass `--implementer-provider`, `--reviewer-provider`, and `--reviewer-identity`; deploy never chooses an Agent based on configuration order. A human must also explicitly pass `--authorize-repository OWNER/REPO` when enrolling or re-enrolling a Consumer. The option must exactly match `--repo`; it records the human enrollment decision in the machine-local runtime config. An existing authorization for another repository is never changed by deploy.

Repository changes are published on a deterministic maintenance branch and opened as a PR when the configured GitHub client has the required write permission. The command never writes the Consumer base branch. If the GitHub App cannot publish the PR, output contains the complete PLAN and a structured `maintenance_pr_required` action; finish that exact PLAN through an authorized GitHub connector, merge the maintenance PR, and rerun `deploy`. Repeated runs adopt the same open PR and use existing repository/runtime/launcher facts to resume. Once the manifest is merged, `deploy` prepares `~/.vccp/runtimes/<full-sha>/`, performs a non-locking machine preflight, then stops/reconciles/qualifies/starts/verifies the native host. An unchanged running macOS LaunchAgent is a no-op; its embedded manifest/config digests must still match the current local files.

`node scripts/vccp.mjs status --repo OWNER/REPO --path /path/to/consumer --runtime-config /absolute/path/to/consumer-runtime.json` is the read-only deployment view. Its overall status is `READY`, `NEEDS_ACTION`, or `BLOCKED`; repository, machine, and runtime facts explain the result. A stopped or uninstalled Consumer needs action. A running owner on the wrong revision or a blocked repository is blocked. `READY` requires the exact clean runtime to be running with matching manifest/config digests and successful machine qualification. External Reviewer Tasks and repository policies remain explicitly `unverified` when the current GitHub connection cannot read them.

### Unified `vccp remove`

Use `node scripts/vccp.mjs remove --repo OWNER/REPO --path /path/to/consumer --runtime-config /absolute/path/to/consumer-runtime.json`. The runtime config's `target_repository` and `workspace` must identify the same Consumer and checkout. Remove derives a fresh ownership plan. It creates or adopts a deterministic maintenance PR for the valid VCCP manifest, installation lock, and only unchanged files whose ownership and hashes are proven by that lock. Consumer-owned or drifted files are retained; ambiguous ownership blocks removal. After merge, update the `--path` checkout to the merged base revision and rerun the same command to check for active local executions, stop and verify the Consumer Runner and RuntimeLock, remove its LaunchAgent, and unregister its Consumer-specific machine bindings. A later run is a no-op.

Remove retains GitHub labels, branch protection/rulesets, Issues, PRs, review history, shared provider applications and credentials, the SQLite runtime database, and shared exact-revision runtime checkouts. It does not cancel work; an active Implementer or Local Reviewer execution blocks machine removal. macOS LaunchAgents are supported. Windows host removal remains blocked until a verified Sidecar remover exists. After removal, `status` reports `NEEDS_ACTION` with repository `not_installed`.

An infrastructure or VCCP-revision PR is a maintenance change, not automatically a product PR. The product Reviewer requires its normal Implementer author and linked Frozen Issue preconditions; when those do not fit, it should skip without formal review. Do not create another Reviewer, invent a Frozen Issue, allow a Bot to review itself, or alter the product Reviewer contract to force maintenance through product automation. Prefer an independent human review. GitHub does not allow a PR author to approve their own PR; do not retry an `APPROVE` that GitHub rejects with HTTP 422 as if it were a plugin failure.

If the repository permissions make independent review impossible, a one-time administrator maintenance merge is allowed only when the maintainer explicitly authorizes it and the PR diff, expected files, exact current head SHA, and checks on that SHA have been re-read. Guard the merge with GitHub CLI's `--match-head-commit` option, for example:

```sh
gh pr merge 123 --squash --admin --match-head-commit EXPECTED_HEAD_SHA
```

Use `--admin` only when the explicitly authorized maintenance operation must bypass an unmet repository requirement. This exception is not a product-flow merge mechanism and does not change the normal-flow rule forbidding admin bypass. See the [GitHub CLI `gh pr merge` reference](https://cli.github.com/manual/gh_pr_merge).

### Reviewer and Dispatcher are different paths

The Dispatcher discovers Frozen Issues through the production Runtime's 30-second polling loop and launches the Implementer. Reviewer is a separate ChatGPT Work GitHub Webhook Task triggered by PR `opened`, `ready_for_review`, and `synchronize` events. VCCP does not require a fully webhook-driven lifecycle: Reviewer events do not imply that Dispatcher polling must be replaced. Do not add a webhook receiver, public endpoint, tunnel, queue, event gateway, or cloud function to this deployment path.

The Reviewer Task is an external state. Reuse an existing Task for this repository when verified; create one only when none exists. If it cannot be inspected, leave its status unverified. An empty repository webhook listing does not prove that a ChatGPT Work Task is absent.

### Readiness, Runner modes, and logs

Run readiness only while the production Runner is stopped. Readiness is a pre-start qualification and attempts the same local process lock; if a running Dispatcher owns that lock, readiness may report not ready. That lock is single-machine exclusion only; it does not coordinate a Windows host with a Mac host.

The Runner supports `--manifest`, `--runtime-config`, `--repo`, `--one-cycle`, `--continuous`, and `--poll-interval`. `--one-cycle` performs real production orchestration: it can claim an Issue, change coordination labels, dispatch work, and advance recovery/lifecycle state. It is not a dry-run or a harmless health check. Use `readiness` for pre-start qualification.

Continuous idle cycles do not emit routine logs. Actionable discovery and lifecycle failures are emitted to stderr as JSON while the loop remains alive and retries on a later cycle. Production acceptance should verify: the expected process is alive; it holds the local RuntimeLock (the Runner acquires it before building the runtime and holds it throughout the loop); discovery failures appear in stderr; and the process can restart and resume polling. A `RUNNING` label or a successful readiness result alone is insufficient evidence of discovery or end-to-end work.

### macOS LaunchAgent environment

A GUI or `launchd` process does not inherit the same shell environment as an interactive Terminal. Configure the absolute Python executable, `WorkingDirectory`, `PYTHONPATH`/module import path, required `PATH`, manifest path, and runtime-config path explicitly. Use machine-local paths and credentials; never copy a user's path into the Consumer repository. A generic per-user LaunchAgent template is:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.example.vccp.consumer</string>
  <key>ProgramArguments</key><array>
    <string>/absolute/path/to/python3</string>
    <string>-m</string><string>vccp_runtime.runner</string>
    <string>--manifest</string><string>/absolute/path/to/consumer/.github/control-plane.yml</string>
    <string>--runtime-config</string><string>/absolute/path/to/consumer-runtime.json</string>
    <string>--repo</string><string>OWNER/REPO</string>
    <string>--continuous</string><string>--poll-interval</string><string>30</string>
  </array>
  <key>WorkingDirectory</key><string>/absolute/path/to/vccp-checkout</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/absolute/path/to/provider-bin:/usr/bin:/bin</string>
    <key>PYTHONPATH</key><string>/absolute/path/to/vccp-checkout</string>
  </dict>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/absolute/path/to/logs/vccp.stdout.log</string>
  <key>StandardErrorPath</key><string>/absolute/path/to/logs/vccp.stderr.log</string>
</dict></plist>
```

Replace every example value. Confirm the job is loaded and its process/logs are the expected ones after installation. `WorkingDirectory` and `ProgramArguments` are `launchd.plist` settings; see the [launchd property-list reference](https://keith.github.io/xcode-man-pages/launchd.plist.5.html).

### Windows host

Use the Consumer's existing, verified resident host mechanism. This repository does not define a standard Windows service/Sidecar installer; do not copy another project's historical Sidecar setup or build a cross-platform installer. The minimum process command boundary is:

```powershell
& "C:\absolute\path\to\python.exe" -m vccp_runtime.runner `
  --manifest "C:\absolute\path\to\consumer\.github\control-plane.yml" `
  --runtime-config "C:\absolute\path\to\consumer-runtime.json" `
  --repo OWNER/REPO --continuous --poll-interval 30
```

The resident host must preserve the configured working directory, import path, and required `PATH`, and must expose stdout/stderr for operations.

### Host migration and single-owner rule

One Consumer has one authorized production Dispatcher owner. RuntimeLock prevents two VCCP processes from using the same local runtime identity on one machine; it is not a distributed lock and does not protect a Mac-to-Windows migration.

Perform takeover in this order:

1. Stop the old host's Dispatcher through its resident host mechanism.
2. Confirm no old resident Runner process remains.
3. Inspect active leases, attempts, and in-flight PR/Issue work using the existing runtime state and current GitHub facts.
4. Resolve or wait for in-flight work before takeover. Continue only when no active work remains or its ownership has been safely resolved.
5. Configure the new host and its machine-local credentials and paths.
6. Keep the new production Runner stopped and run readiness against the Consumer's merged main and exact pinned VCCP revision.
7. Start the new resident Dispatcher.
8. Verify one expected process owns the local lock, logs are accessible, and restart behavior is understood.

Never run old and new hosts as simultaneous owners, and do not add distributed election or cross-machine locking as a migration shortcut.

### Identity and production evidence

The GitHub App ID identifies the App and belongs in machine-local runtime config. The Bot User ID is a GitHub account identifier; the manifest's `repository.implementer_authors` instead takes author login text such as `some-app[bot]`. A human infrastructure operator account is a separate identity. Do not substitute one for another.

Record production evidence by level: **1** repository ready; **2** machine ready; **3** Dispatcher running; **4** Issue discovery observed; **5** full zero-touch E2E through native merge and closeout. Do not promote Level 2 or Level 3 evidence to Level 5. Do not create a fake product Issue to claim Level 5; record full Mac zero-touch E2E as `PENDING_REAL_PRODUCT_ISSUE` until a real product request exercises it.
