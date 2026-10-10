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

`vccp_runtime.adapters.build_runtime(manifest, local_config)` wires a schema-v2 consumer manifest to the existing `RuntimeCore`. Keep local settings outside `.github/control-plane.yml`; provide `database_path`, `workspace`, `owner_id`, `antigravity_executable`, and a `github_app` block pointing to a current-user DPAPI-protected PEM file in machine-local configuration.

Concrete implementers enter the runtime through `vccp_runtime.providers.ProviderRouter`, which is the only `ImplementerPort` `RuntimeCore` sees. Machine-local configuration may list ordered `providers` (a `key`, a `type`, and provider settings); the first entry is primary and later entries are ordered fallbacks. An existing AntiGravity-only configuration without a `providers` list stays valid as one primary provider. Routing is fail-closed: only a provider that proved it did not start work may fall through, `UNKNOWN` never reroutes, and the provider that owned a launch is stored in `attempts.implementer_provider` so restart progress observation never guesses a provider.

Two provider types are implemented. `antigravity` launches `language_server.exe agentapi new-conversation <prompt>`. `claude_code` launches the installed Claude Code CLI as `claude --bg <prompt>`, passing the same generic Implementer Prompt: the Frozen Issue stays the only task specification, and initial work, same-PR repair, controlled GitHub writes, and controlled push follow the identical contract. The launch also carries an inline settings payload that disables the CLI's git-worktree background isolation, because a VCCP implementer must work in the configured consumer checkout, and `--permission-mode bypassPermissions` so an unattended session can run. Claude's durable execution identity is the session UUID the CLI assigns, resolved through `claude agents --json --cwd <workspace>`; that listing is also the supported surface used to re-observe the session after a VCCP restart, gated to the exact stored session so another session or provider is never observed.

Claude launch outcomes stay conservative. A guard that fails to install or an executable that cannot be created proves the work did not start, so the launch is `DEFINITELY_NOT_STARTED` and the next provider may be tried. Once the CLI has been invoked, a non-zero exit, a timeout, a missing acknowledgement, or an unresolvable session identity are all `UNKNOWN`: they cannot disprove that Claude started, so they are recorded with `provider=claude_code` and never route onward. Each provider installs its own guard under `vccp-control/<attempt id>/<provider key>`, so a fallback in the same attempt never collides with the primary's guard, wrappers, or GH config. The App provider mints repo-scoped installation tokens in memory; long-lived tokens and private keys are not stored in the runtime database.

Before AgentAPI starts, `WorkspaceWriteGuard` installs a repo-local pre-push hook in Git metadata and generates attempt-scoped `app-gh` and `app-git-push` wrappers. Direct `git push` is blocked. VCCP's bounded writer supports coordination-label edits and linked PR creation; the push backend verifies the workspace, remote, local SHA, destination branch, App authentication, and resulting remote SHA. The AgentAPI environment uses an isolated `GH_CONFIG_DIR` and excludes inherited GitHub tokens. AntiGravity launch uses `language_server.exe agentapi new-conversation <prompt>` from the configured workspace; only a UUID conversation ID is accepted as confirmation. Launch uncertainty is recorded by `RuntimeCore` using its existing tri-state. This wiring constructs adapters only; it does not start a poller or daemon.
