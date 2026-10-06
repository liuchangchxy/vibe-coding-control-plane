# Consumer Onboarding

This guide connects one GitHub repository to the shared Control Plane contract. Every consumer owns its application CI and its own Reviewer task instance.

## 1. Configure the consumer

Copy `templates/control-plane.yml` into the consumer repository (for example `.github/control-plane.yml`). Replace every `REPLACE_ME`; set `required_checks` to that repository's actual branch-protection check names and accepted success conclusions. Do not copy EasyExam check names unless they independently apply to that project.

Protect the configured base branch. Require the configured checks and an authorized approval, enable native Auto-merge, and prevent normal Implementer identities from direct merge and admin bypass. Configure the desired native merge method.

Create the five coordination labels with the names and meanings in the manifest. Treat `frozen-spec` as a separate classification label. An executing Issue may have only one coordination label. Do not create labels that duplicate native PR, review, check, or merge state.

## 2. Install completion cleanup

Copy `templates/coordination-label-cleanup.yml` to the consumer's `.github/workflows/` directory and `scripts/cleanup-coordination-labels.js` to `.github/scripts/`. Review the workflow permissions and keep its token scoped to reading repository contents and editing Issue labels. The workflow runs on Issue close; it removes active labels and leaves `infra-blocked` or `needs-human` in place.

Copy and customize `templates/pull_request_template.md` so each PR identifies its one linked Frozen Issue, reports scope, and records actual check results. The template is a reporting aid; GitHub state remains authoritative.

## 3. Create a per-repository Reviewer task

In ChatGPT Work, create a GitHub webhook task specifically for this consumer repository. Use `templates/reviewer-task-prompt.md` as the canonical instructions and fill in the repository-specific manifest values. Do not create one central task that listens across repositories.

Configure native GitHub events that start a fresh run for relevant PR activity, commit updates, and required-check completion. The task should exit without a formal review when prerequisites are pending; it must not poll. Verify the chosen Work webhook integration actually delivers these events for this repository before relying on it. This repository specifies the contract, not a proof that a particular event integration or Dispatcher is reliable.

## 4. Operational boundary

On every run, Reviewer reloads the Issue, PR, labels, checks, reviews, and merge state from GitHub. It does not rely on previous run memory. Formal reviews must bind to the exact current head SHA. CI passing alone is not approval. Repairs update the same PR and stop after three formal `REQUEST_CHANGES` rounds; a further rejection moves the Issue to `needs-human`.

`vibe-coding-control-plane` is the canonical source for these automation contracts and templates. `vibe-coding-starter` is a consumer/bootstrapper that may automate future onboarding. Existing projects such as EasyExam, DaySpark, and Zhanghui can migrate through this guide. Control Plane does not depend on the starter.
