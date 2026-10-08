# Vibe Coding Control Plane

Reusable GitHub-native contracts and templates for AI-assisted development across repositories.

This repository is the canonical source for the automation contract: issue coordination states, implementer/reviewer/human boundaries, exact-head review, same-PR repair, terminal stops, and native Auto-merge. It does not prescribe application tests. Each consumer repository declares its own required checks in its Control Plane manifest.

## Lifecycle

```text
Frozen Issue (frozen-spec + agent-ready)
→ Dispatcher/Sidecar claim (agent-working)
→ Implementer PR
→ consumer-defined required checks for the current head SHA
→ per-repository ChatGPT Work Reviewer task
→ native APPROVE or REQUEST_CHANGES
→ GitHub native Auto-merge
```

Formal `REQUEST_CHANGES` reopens work on the same PR. VCCP v2 gives one budget of at most three automated repairs to the Dispatcher, shared by implementation-attributable CI failures and Reviewer rejections. `infra-blocked` and `needs-human` are terminal until a human resolves them.

## Use this repository

Start with [consumer onboarding](docs/ONBOARDING.md). The Node CLI produces a read-only JSON PLAN, applies only explicitly safe changes from a saved plan, and audits committed consumer state. It never creates ChatGPT Work Tasks or writes branch protection.

`vibe-coding-control-plane` is the automation/control-plane canonical source. `vibe-coding-starter` is the consumer/bootstrapper. Future projects can be connected by the starter; existing projects such as EasyExam, DaySpark, and Zhanghui onboard through migration. This repository does not depend on the starter.

EasyExam is documented as reference evidence in [EASYEXAM_REFERENCE.md](docs/EASYEXAM_REFERENCE.md), not as a source of universal test commands or application-specific defaults.

## Contents

- [Control Plane contract](docs/CONTROL_PLANE_CONTRACT.md)
- [Onboarding](docs/ONBOARDING.md)
- [EasyExam reference evidence](docs/EASYEXAM_REFERENCE.md)
- `vccp_runtime/` — SQLite execution core plus production adapters with fake-driven tests
- `templates/` — consumer manifest, Reviewer task prompt, PR template, and cleanup workflow
- `scripts/`, `lib/onboarding/`, `schemas/`, and `tests/` — onboarding CLI, shared planner, schemas, and regression tests

## Non-daemon runtime wiring

`vccp_runtime.adapters.build_runtime(manifest, local_config)` wires a schema-v2 consumer manifest to the existing `RuntimeCore`. Keep local settings outside `.github/control-plane.yml`; provide `database_path`, `workspace`, `owner_id`, `antigravity_executable`, `app_gh_executable`, `app_git_push_executable`, and `github_read_token_env` in machine-local configuration. The read token is resolved from the named environment variable and is never persisted by the runtime.

The configured `app-gh` executable is invoked with native `gh issue edit` label arguments and is rejected if configured as ordinary `gh`. Before AgentAPI starts, `WorkspaceWriteGuard` installs a repo-local pre-push hook in Git metadata; direct `git push` is blocked, while a generated shim invokes the configured `app-git-push` with the controlled hook bypass. The AgentAPI environment uses an empty `GH_CONFIG_DIR` and excludes inherited GitHub tokens, so ordinary `gh` cannot use host credentials. The Implementer receives the configured `app-gh` and guarded push paths in its prompt and `VCCP_APP_GH` / `VCCP_APP_GIT_PUSH` environment variables. AntiGravity launch uses `language_server.exe agentapi new-conversation <prompt>` from the configured workspace; only a UUID conversation ID is accepted as confirmation. Launch uncertainty is recorded by `RuntimeCore` using its existing tri-state. This wiring constructs adapters only; it does not start a poller or daemon.
