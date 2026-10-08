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
- `templates/` — consumer manifest, Reviewer task prompt, PR template, and cleanup workflow
- `scripts/`, `lib/onboarding/`, `schemas/`, and `tests/` — onboarding CLI, shared planner, schemas, and focused tests
