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

PLAN also checks existing local GitHub workflows for an overlapping coordination-label cleanup capability on `issues.closed`. It follows local script references from those workflows and blocks when such a handler mutates VCCP coordination labels but its compatibility cannot be established. VCCP requires every active coordination label to be removed after an Issue closes, including when a terminal label is present; terminal labels themselves remain as evidence. The only compatibility currently proven automatically is the exact VCCP-managed cleanup workflow. A conflict blocks PLAN and AUDIT, and APPLY re-plans before writing, so it cannot silently overwrite or install alongside an incompatible handler. Resolve the consumer-owned automation and rerun PLAN; do not treat a shared event or a similar workflow name as proof of compatibility.

APPLY creates only missing standard label identities. Existing labels are never renamed, edited, or deleted; differing colors/descriptions are audit warnings. Treat `frozen-spec` as a separate classification label. An executing Issue may have only one coordination label.

## 2. Install completion cleanup

APPLY installs a thin workflow adapter referencing VCCP's central cleanup Action at the exact full source commit SHA in the manifest. The adapter needs `issues: write`; the Action removes active labels and preserves terminal labels. Its implementation remains central and is versioned with VCCP.

The optional `.github/PULL_REQUEST_TEMPLATE/vccp.md` is only a reporting aid. If the consumer already has a default PR template, onboarding leaves it untouched and does not force a second template. The Reviewer contract itself enforces exactly one linked Frozen Issue and current PR state.

## 3. Create a per-repository Reviewer task

In ChatGPT Work, create a GitHub webhook Task specifically for this consumer repository and attach the rendered `.github/vccp/reviewer-task-prompt.md`. This is an external step: local CLI cannot create or verify the Task or claim it exists. The Task starts on `opened`, `ready_for_review`, and `synchronize`; within the same event-triggered run it treats both an unobserved check (which may not have been created yet) and a queued/in-progress/pending check as not-yet-terminal and continues waiting. It rechecks the captured head SHA after every wait, and only reports a still-absent check as `missing/timeout` at the run deadline/platform limit. A terminal unaccepted conclusion stops approval. No check-completion trigger or separate polling mechanism is used.

Branch protection and rulesets are read-only audited and remain an external maintainer step. Onboarding never replaces these objects. The existing Python Generic Runtime is the runtime wiring boundary; no future Dispatcher registration is required by repository onboarding.

## 4. Operational boundary

For example, from a clean VCCP checkout, obtain its source revision with `git rev-parse HEAD` and place that exact SHA into `onboarding.json`. Then run `node scripts/onboard-consumer.mjs plan --repo OWNER/REPO --path /path/to/consumer --input onboarding.json > /tmp/vccp-plan.json`, inspect the file, and run `node scripts/onboard-consumer.mjs apply --repo OWNER/REPO --path /path/to/consumer --plan /tmp/vccp-plan.json`. `node scripts/onboard-consumer.mjs audit --repo OWNER/REPO --path /path/to/consumer` emits a repository audit. The CLI verifies its runtime checkout matches the desired VCCP revision so generated files and central Action references cannot claim a different source revision. No command commits, pushes, creates a PR, or modifies a consumer outside the selected checkout.

## 5. Machine-local runtime activation

Repository onboarding writes `.github/control-plane.yml`, the shared consumer contract. Machine-local activation is separate: a JSON file on the runtime host supplies local paths, the SQLite location, owner identity, and the name of the environment variable that contains a GitHub read token. Never put these values in `.github/control-plane.yml`, generated onboarding files, or another repository-managed artifact. In particular, the token value is not stored in the JSON file.

Example `C:/Users/<user>/.vccp/consumer-runtime.json` (replace every example path locally):

```json
{
  "database_path": "C:/Users/<user>/.vccp/state/consumer.sqlite",
  "workspace": "C:/work/consumer",
  "owner_id": "local-runtime-owner",
  "antigravity_executable": "C:/Users/<user>/AppData/Local/AntiGravity/language_server.exe",
  "app_gh_executable": "C:/tools/app-gh.exe",
  "app_git_push_executable": "C:/tools/app-git-push.exe",
  "github_read_token_env": "VCCP_GITHUB_READ_TOKEN",
  "launch_timeout_seconds": 60,
  "recovery_timeout_seconds": 900,
  "lifecycle_timeouts": {
    "waiting_ci": 3600,
    "waiting_review": 86400,
    "waiting_merge": 86400
  }
}
```

The database parent directory must already exist and be writable. The workspace `origin` must identify the same `OWNER/REPO` passed to the command. The controlled GitHub writer must not be ordinary `gh`. Provide the token through the named environment variable in the runtime process environment; readiness reports only its name and whether a value is present.

Run qualification explicitly:

```sh
node scripts/onboard-consumer.mjs readiness --repo OWNER/REPO --path /path/to/consumer --runtime-config /path/to/consumer-runtime.json
```

Readiness validates the local schema-v2 repository contract, workspace identity, config, executable and database-directory prerequisites, credential source presence, then constructs the existing production adapter/runtime graph using a temporary SQLite file in the configured database directory. It deletes that temporary state afterwards. It does not dispatch Issues, mutate GitHub labels, create PRs, start repairs, or launch AntiGravity conversations. Its credential and writer collaborators are inert construction fakes; this qualifies wiring and local prerequisites, not token authorization or a production dispatch.

`repository_status` and `runtime_activation.status` are independent. Repository drift/blockage is `repository_blocked`; a schema-v1 manifest is `repository_upgrade_required`; a clean repository is `repository_ready`. PLAN, APPLY, and AUDIT without a supplied activation config report `runtime_activation.status: unknown`. AUDIT can qualify a supplied config with `--runtime-config`. Readiness returns `machine_activation_ready` only when activation succeeds; missing config/prerequisites or runtime-construction failure returns `machine_activation_blocked` with blocker reasons and exit code 2. This status describes the current machine's construction readiness, not an always-running daemon or completed business operation.

To actually construct and run the existing runtime from Python, load the manifest and local JSON config and pass them to `vccp_runtime.adapters.build_runtime(manifest, local_config)`. The caller may then explicitly invoke `vccp_runtime.orchestrator.run_once(...)` when it intends to perform one runtime step. Readiness never calls `run_once()`.

Machine-readable repository overall states and process exit codes are: `repository_changes_planned` (0), `repository_ready` (0), `repository_upgrade_required` (0), `repository_blocked` (2), `command_failed` (3), and `stale_plan` (4). `repository_upgrade_required` identifies a recognized legacy schema v1 manifest and does not authorize APPLY to rewrite it. A zero from PLAN means planning completed successfully; inspect `overall_status` and `items` to see whether APPLY changes remain. Repository readiness does not imply machine-local runtime activation; read `runtime_activation.status` or run the readiness command.

For future new projects, `vibe-coding-starter` calls this same CLI/API after creating the repository. It must not implement a second onboarding path.

`vibe-coding-control-plane` is the canonical source for these automation contracts and templates. `vibe-coding-starter` is a consumer/bootstrapper that may automate future onboarding. Existing projects such as EasyExam, DaySpark, and Zhanghui can migrate through this guide. Control Plane does not depend on the starter.

Node tooling owns PLAN, APPLY, AUDIT, manifest generation/validation, and Reviewer contract installation. Python owns activation qualification and construction of the Generic Runtime, plus claim/ownership, dispatch, repair admission, durable execution state, reconciliation, watchdog, and one-shot exact-head CI / Review / native-merge observation. The shared manifest holds consumer policy; local JSON holds machine configuration. These boundaries prevent host-specific paths and credential data from becoming shared repository contract.
