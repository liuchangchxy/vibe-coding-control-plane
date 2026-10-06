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

Formal `REQUEST_CHANGES` reopens work on the same PR. After at most three formal repair rounds, another rejection moves the Issue to `needs-human` and stops automation. `infra-blocked` and `needs-human` are terminal until a human resolves them.

## Use this repository

Start with [the consumer onboarding specification](docs/ONBOARDING.md), copy and fill in [the manifest](templates/control-plane.yml), then create a separate ChatGPT Work GitHub webhook task for that repository using [the canonical reviewer prompt](templates/reviewer-task-prompt.md). The task is per repository; it must rebuild its decision from GitHub durable state on every run.

`vibe-coding-control-plane` is the automation/control-plane canonical source. `vibe-coding-starter` is the consumer/bootstrapper. Future projects can be connected by the starter; existing projects such as EasyExam, DaySpark, and Zhanghui onboard through migration. This repository does not depend on the starter.

EasyExam is documented as reference evidence in [EASYEXAM_REFERENCE.md](docs/EASYEXAM_REFERENCE.md), not as a source of universal test commands or application-specific defaults.

## Contents

- [Control Plane contract](docs/CONTROL_PLANE_CONTRACT.md)
- [Onboarding](docs/ONBOARDING.md)
- [EasyExam reference evidence](docs/EASYEXAM_REFERENCE.md)
- `templates/` — consumer manifest, Reviewer task prompt, PR template, and cleanup workflow
- `scripts/` and `tests/` — reusable coordination-label cleanup and its focused tests
