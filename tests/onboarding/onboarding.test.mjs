import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { createOnboardingPlan } from "../../lib/onboarding/plan.mjs";
import { applyOnboardingPlan, auditConsumer } from "../../lib/onboarding/apply.mjs";
import { runOnboardingCli } from "../../lib/onboarding/cli.mjs";
import { createManifest, parseManifest, stringifyManifest, validateManifest } from "../../lib/onboarding/manifest.mjs";

const SOURCE = "liuchangchxy/vibe-coding-control-plane";
const REV_A = "a".repeat(40);
const REV_B = "b".repeat(40);
const REPO = "example/consumer";

function validInput(revision = REV_A) {
  return {
    vccp: { source: SOURCE, revision },
    repository: { base_branch: "main", implementer_authors: ["builder[bot]"] },
    reviewer: {
      required_checks: [
        { name: "Consumer CI", accepted_conclusions: ["success"] },
        { name: "Consumer Security", accepted_conclusions: ["success"] },
      ],
    },
    merge: { method: "squash" },
    reporting: { pull_request_template: false },
  };
}

class FakeGitHub {
  constructor() {
    this.repository = {
      full_name: REPO,
      default_branch: "main",
      branches: ["main"],
    };
    this.reachableRevisions = new Set([REV_A, REV_B]);
    this.labels = [];
    this.labelCreates = [];
    this.protection = { readable: true, configured: false };
    this.rulesets = { readable: true, items: [] };
  }

  async getRepository() { return this.repository; }
  async getBranch(_repo, name) { return name === "main" ? { name, commit: { sha: "c".repeat(40) } } : null; }
  async getCommit(source, sha) { return source === SOURCE && this.reachableRevisions.has(sha); }
  async listLabels() { return structuredClone(this.labels); }
  async createLabel(_repo, label) {
    this.labelCreates.push(label.name);
    this.labels.push({ ...label });
  }
  async getBranchProtection() { return structuredClone(this.protection); }
  async listRulesets() { return structuredClone(this.rulesets); }
}

async function withRepo(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "vccp-onboard-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    await fn(root);
  } finally { await rm(root, { recursive: true, force: true }); }
}

test("fresh input creates a read-only plan pinned to a reachable canonical revision", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });

    assert.equal(plan.overall_status, "repository_changes_planned");
    assert.equal(plan.exit_code, 0);
    assert.equal(plan.manifest.vccp.revision, REV_A);
    assert.equal(plan.manifest.schema_version, 2);
    assert.equal(plan.manifest.issue_contract.max_automated_repairs, 3);
    assert.equal(plan.items.find((item) => item.id === "manifest").status, "create");
    assert.equal(plan.items.find((item) => item.id === "labels").status, "create");
    assert.equal(plan.external_steps.find((item) => item.id === "reviewer-task").status, "external-step");
    assert.equal(plan.external_steps.some((item) => item.id === "dispatcher"), false);
    assert.equal(plan.runtime_activation.status, "unknown");
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));
  });
});

test("clean consumer plans VCCP cleanup creation and an exact managed cleanup is idempotent", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const first = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(first.items.find((entry) => entry.path === ".github/workflows/vccp-coordination-label-cleanup.yml").status, "create");
    await applyOnboardingPlan({ plan: first, root, github });

    const second = await createOnboardingPlan({ repo: REPO, root, github });
    assert.equal(second.overall_status, "repository_ready");
    assert.equal(second.items.find((entry) => entry.path === ".github/workflows/vccp-coordination-label-cleanup.yml").status, "satisfied");
  });
});

test("EasyExam-shaped terminal-preserving issues.closed cleanup blocks plan, apply, and audit", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    const priorPlan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    const cleanupWorkflow = [
      "name: Coordination Label Cleanup",
      "on:",
      "  issues:",
      "    types: [closed]",
      "jobs:",
      "  cleanup:",
      "    steps:",
      "      - uses: actions/github-script@v7",
      "        with:",
      "          script: |",
      '            const cleanup = require(`${process.env.GITHUB_WORKSPACE}/.github/scripts/cleanup-coordination-labels`);',
    ].join("\n");
    await writeFile(path.join(workflowDir, "coordination-label-cleanup.yml"), cleanupWorkflow);
    await writeFile(path.join(scriptDir, "cleanup-coordination-labels.js"), `const ACTIVE_LABELS = ["agent-ready", "agent-working", "changes-requested"];\nconst TERMINAL_LABELS = ["infra-blocked", "needs-human"];\nasync function cleanup(issue, removeLabel) {\n  const labels = new Set(issue.labels.map(({ name }) => name));\n  if (TERMINAL_LABELS.some((name) => labels.has(name))) return { preservedTerminalState: true, removed: [] };\n  for (const name of ACTIVE_LABELS) if (labels.has(name)) await removeLabel(name);\n}\n`);

    const staleApply = await applyOnboardingPlan({ plan: priorPlan, root, github });
    assert.equal(staleApply.overall_status, "stale_plan");
    assert.deepEqual(staleApply.applied, []);
    assert.equal(github.labelCreates.length, 0);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_blocked");
    const conflict = plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup");
    assert.equal(conflict.status, "conflict");
    assert.equal(conflict.event, "issues.closed");
    assert.match(conflict.reason, /preserves active coordination labels/);
    assert.match(conflict.reason, /VCCP requires active labels to be removed after every Issue close/);

    const applied = await applyOnboardingPlan({ plan, root, github });
    assert.equal(applied.overall_status, "repository_blocked");
    assert.deepEqual(applied.applied, []);
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));

    await mkdir(path.join(root, ".github"), { recursive: true });
    await writeFile(path.join(root, ".github/control-plane.yml"), stringifyManifest(createManifest(validInput())));
    const audit = await auditConsumer({ repo: REPO, root, github });
    assert.equal(audit.overall_status, "repository_blocked");
    assert.ok(audit.items.some((entry) => entry.id === "capability:coordination-label-cleanup" && entry.status === "conflict"));
  });
});

test("direct-only repository-relative cleanup reference blocks an incompatible issues.closed handler", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    await writeFile(path.join(workflowDir, "coordination-label-cleanup.yml"), `name: Coordination Label Cleanup\non:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/cleanup-coordination-labels.js\n`);
    await writeFile(path.join(scriptDir, "cleanup-coordination-labels.js"), `const ACTIVE_LABELS = ["agent-ready", "agent-working", "changes-requested"];\nconst TERMINAL_LABELS = ["infra-blocked", "needs-human"];\nasync function cleanup(issue, removeLabel) {\n  const labels = new Set(issue.labels.map(({ name }) => name));\n  if (TERMINAL_LABELS.some((name) => labels.has(name))) return { preservedTerminalState: true, removed: [] };\n  for (const name of ACTIVE_LABELS) if (labels.has(name)) await removeLabel(name);\n}\n`);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_blocked");
    assert.equal(plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup").path, ".github/workflows/coordination-label-cleanup.yml");
  });
});

test("unresolved coordination cleanup behavior fails closed for an issues.closed handler", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    await mkdir(workflowDir, { recursive: true });
    await writeFile(path.join(workflowDir, "coordination-label-cleanup.yml"), `name: Coordination Label Cleanup\non:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/cleanup-coordination-labels\n`);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_blocked");
    const conflict = plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup");
    assert.equal(conflict.status, "conflict");
    assert.equal(conflict.event, "issues.closed");
    assert.match(conflict.reason, /behavior source that cannot be resolved/);
    assert.equal(conflict.path, ".github/workflows/coordination-label-cleanup.yml");
  });
});

test("literal extensionless cleanup script takes priority over the .js fallback", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    await writeFile(path.join(workflowDir, "coordination-label-cleanup.yml"), `name: Coordination Label Cleanup\non:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/cleanup-coordination-labels\n`);
    await writeFile(path.join(scriptDir, "cleanup-coordination-labels"), `const ACTIVE_LABELS = ["agent-ready"];
const TERMINAL_LABELS = ["needs-human"];
if (TERMINAL_LABELS.some((name) => labels.has(name))) return { removed: [] };
for (const name of ACTIVE_LABELS) await removeLabel(name);
`);
    await writeFile(path.join(scriptDir, "cleanup-coordination-labels.js"), "module.exports = () => {};\n");

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_blocked");
    assert.match(plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup").reason, /preserves active coordination labels/);
  });
});

test("an overlapping cleanup with unproven behavior blocks instead of assuming compatibility", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    await writeFile(path.join(workflowDir, "issue-close.yml"), `on:\n  issues: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node \"\${process.env.GITHUB_WORKSPACE}/.github/scripts/cleanup.js\"\n`);
    await writeFile(path.join(scriptDir, "cleanup.js"), `const ACTIVE_LABELS = ["agent-ready"];\nawait github.rest.issues.removeLabel({ name: ACTIVE_LABELS[0] });\n`);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_blocked");
    assert.match(plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup").reason, /cannot be proven compatible/);
  });
});

test("v2 manifest contract accepts only the shared frozen repair budget", () => {
  const manifest = createManifest(validInput());
  assert.deepEqual(validateManifest(manifest), []);
  const legacyField = structuredClone(manifest);
  legacyField.issue_contract.max_request_changes_rounds = 3;
  assert.ok(validateManifest(legacyField).some((error) => error.includes("max_request_changes_rounds is not supported")));
  const wrongLimit = structuredClone(manifest);
  wrongLimit.issue_contract.max_automated_repairs = 2;
  assert.ok(validateManifest(wrongLimit).some((error) => error.includes("must be 3")));
  const legacyVersion = structuredClone(manifest);
  legacyVersion.schema_version = 1;
  assert.ok(validateManifest(legacyVersion).some((error) => error.includes("max_automated_repairs is not supported")));
});

test("schema, generated manifest, onboarding guide, contract, and Reviewer prompt agree on v2", async () => {
  const schema = JSON.parse(await readFile(new URL("../../schemas/control-plane.schema.json", import.meta.url), "utf8"));
  const template = await readFile(new URL("../../templates/control-plane.yml", import.meta.url), "utf8");
  const onboarding = await readFile(new URL("../../docs/ONBOARDING.md", import.meta.url), "utf8");
  const contract = await readFile(new URL("../../docs/CONTROL_PLANE_CONTRACT.md", import.meta.url), "utf8");
  const reviewer = await readFile(new URL("../../templates/reviewer-task-prompt.md", import.meta.url), "utf8");
  assert.equal(schema.properties.schema_version.const, 2);
  assert.ok(schema.properties.issue_contract.required.includes("max_automated_repairs"));
  assert.equal(schema.properties.issue_contract.properties.max_automated_repairs.const, 3);
  assert.match(template, /schema_version: 2/);
  assert.match(template, /max_automated_repairs: 3/);
  assert.match(onboarding, /`repository_upgrade_required` \(0\)/);
  assert.match(onboarding, /Python owns activation qualification and construction of the Generic Runtime, plus claim\/ownership, dispatch, repair admission/);
  assert.match(contract, /shared by implementation-attributable CI repairs and Reviewer-caused repairs/);
  assert.match(contract, /Dispatcher admits a candidate/);
  assert.match(contract, /repair:<repo>:pr:<pr_number>:head:<failed_or_rejected_head_sha>/);
  assert.match(contract, /same linked PR and implementation branch/);
  assert.match(contract, /schema v1 is unsupported/);
  assert.match(reviewer, /Dispatcher alone admits or rejects repairs/);
  for (const text of [template, onboarding, contract, reviewer]) {
    assert.doesNotMatch(text, /three formal `?REQUEST_CHANGES`? repair rounds/i);
  }
});

test("schema v1 parses as legacy, preserves stale-plan protection, and never rewrites", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const source = `schema_version: 1\nvccp:\n  source: ${SOURCE}\n  revision: ${REV_A}\nrepository:\n  base_branch: main\n  implementer_authors:\n    - builder[bot]\nissue_contract:\n  frozen_spec_label: frozen-spec\n  active_coordination_labels: [agent-ready, agent-working, changes-requested]\n  terminal_coordination_labels: [infra-blocked, needs-human]\n  max_request_changes_rounds: 3\nreviewer:\n  transport: chatgpt-work-github-webhook-task\n  events: [pull_request.opened, pull_request.ready_for_review, pull_request.synchronize]\n  exact_head_sha_required: true\n  required_checks_completion: same_run_wait_until_terminal\n  required_checks:\n    - name: Consumer CI\n      accepted_conclusions: [success]\n    - name: Consumer Security\n      accepted_conclusions: [success]\nmerge:\n  native_auto_merge: true\n  enable_native_auto_merge_by: reviewer\n  method: squash\n  direct_merge_forbidden: true\n  admin_bypass_forbidden_in_normal_flow: true\nreporting:\n  pull_request_template: false\n`;
    const parsed = parseManifest(source);
    assert.equal(parsed.schema_version, 1);
    await mkdir(path.join(root, ".github"), { recursive: true });
    const target = path.join(root, ".github/control-plane.yml");
    await writeFile(target, source);

    const plan = await createOnboardingPlan({ repo: REPO, root, github });
    assert.equal(plan.overall_status, "repository_upgrade_required");
    assert.deepEqual(plan.legacy_manifest, { schema_version: 1, status: "upgrade_required", generic_runtime_supported: false });
    assert.equal((await readFile(target, "utf8")), source);
    const applied = await applyOnboardingPlan({ plan, root, github });
    assert.equal(applied.overall_status, "repository_upgrade_required");
    assert.deepEqual(applied.applied, []);

    let auditOutput = "";
    let auditErrors = "";
    const auditCode = await runOnboardingCli(["audit", "--repo", REPO, "--path", root], {
      stdout: { write: (text) => { auditOutput += text; } },
      stderr: { write: (text) => { auditErrors += text; } },
      githubFactory: () => github,
      runtimeRevision: REV_B,
    });
    assert.equal(auditCode, 0);
    assert.equal(auditErrors, "");
    assert.equal(JSON.parse(auditOutput).overall_status, "repository_upgrade_required");
    assert.equal((await readFile(target, "utf8")), source);

    const planPath = path.join(root, "legacy-plan.json");
    await writeFile(planPath, JSON.stringify(plan));
    let applyOutput = "";
    let applyErrors = "";
    const applyCode = await runOnboardingCli(["apply", "--repo", REPO, "--path", root, "--plan", planPath], {
      stdout: { write: (text) => { applyOutput += text; } },
      stderr: { write: (text) => { applyErrors += text; } },
      githubFactory: () => github,
      runtimeRevision: REV_B,
    });
    assert.equal(applyCode, 0);
    assert.equal(applyErrors, "");
    assert.equal(JSON.parse(applyOutput).overall_status, "repository_upgrade_required");
    assert.deepEqual(JSON.parse(applyOutput).applied, []);
    assert.equal((await readFile(target, "utf8")), source);
    assert.equal(github.labelCreates.length, 0);

    const changedSource = source.replace(REV_A, REV_B);
    await writeFile(target, changedSource);
    const stale = await applyOnboardingPlan({ plan, root, github });
    assert.equal(stale.overall_status, "stale_plan");
    assert.equal(stale.exit_code, 4);
    assert.deepEqual(stale.applied, []);
    assert.equal((await readFile(target, "utf8")), changedSource);

    const audit = await auditConsumer({ repo: REPO, root, github });
    assert.equal(audit.command, "audit");
    assert.equal(audit.overall_status, "repository_upgrade_required");
    assert.equal((await readFile(target, "utf8")), changedSource);
    assert.equal(github.labelCreates.length, 0);
  });
});

test("apply writes the consumer state and the second plan/apply is a no-op", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const first = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    const applied = await applyOnboardingPlan({ plan: first, root, github });
    assert.equal(applied.overall_status, "repository_ready");
    assert.equal(applied.exit_code, 0);
    assert.equal(github.labelCreates.length, 6);

    const second = await createOnboardingPlan({ repo: REPO, root, github });
    assert.equal(second.overall_status, "repository_ready");
    assert.ok(second.items.every((item) => item.status === "satisfied" || item.status === "external-step"));
    const again = await applyOnboardingPlan({ plan: second, root, github });
    assert.equal(again.overall_status, "repository_ready");
    assert.equal(github.labelCreates.length, 6);
  });
});

test("empty checks and placeholder identities cannot produce a ready plan", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const input = validInput();
    input.reviewer.required_checks = [];
    input.repository.implementer_authors = ["REPLACE_ME"];
    const plan = await createOnboardingPlan({ repo: REPO, root, input, github });
    assert.equal(plan.overall_status, "repository_blocked");
    assert.ok(plan.items.some((item) => item.status === "blocked"));
  });
});

test("optional exact CI failure attribution fields remain schema-v2 compatible", async () => {
  const input = validInput();
  input.reviewer.required_checks[0].implementation_failure_conclusions = ["failure"];
  input.reviewer.required_checks[0].infrastructure_failure_conclusions = ["timed_out"];
  assert.deepEqual(validateManifest(createManifest(input)), []);
  input.reviewer.required_checks[0].implementation_failure_conclusions = [""];
  assert.throws(() => createManifest(input), /implementation_failure_conclusions/);
});

test("revision changes only through an explicit proposal and lock-confirmed upgrade", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const first = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan: first, root, github });

    const implicit = await createOnboardingPlan({ repo: REPO, root, github, currentVccpRevision: REV_B });
    assert.equal(implicit.manifest.vccp.revision, REV_A);
    assert.equal(implicit.items.find((item) => item.id === "manifest").status, "satisfied");

    const proposed = await createOnboardingPlan({ repo: REPO, root, github, revisionProposal: REV_B });
    assert.equal(proposed.manifest.vccp.revision, REV_B);
    assert.equal(proposed.items.find((item) => item.id === "manifest").status, "update");
    await applyOnboardingPlan({ plan: proposed, root, github });
    const installed = await createOnboardingPlan({ repo: REPO, root, github });
    assert.equal(installed.manifest.vccp.revision, REV_B);
  });
});

test("managed artifact drift blocks apply without overwriting consumer edits", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const first = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan: first, root, github });
    const target = path.join(root, ".github/workflows/vccp-coordination-label-cleanup.yml");
    await writeFile(target, "# consumer-owned change\n", "utf8");

    const plan = await createOnboardingPlan({ repo: REPO, root, github, revisionProposal: REV_B });
    assert.equal(plan.items.find((item) => item.path?.endsWith("vccp-coordination-label-cleanup.yml")).status, "conflict");
    const before = await readFile(target, "utf8");
    const result = await applyOnboardingPlan({ plan, root, github });
    assert.equal(result.overall_status, "repository_blocked");
    assert.equal(result.exit_code, 2);
    assert.equal(await readFile(target, "utf8"), before);
  });
});

test("stale plans are rejected before file or label mutations", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await mkdir(path.join(root, ".github"), { recursive: true });
    await writeFile(path.join(root, ".github/control-plane.yml"), "consumer edit\n");
    const result = await applyOnboardingPlan({ plan, root, github });
    assert.equal(result.overall_status, "stale_plan");
    assert.equal(result.exit_code, 4);
    assert.equal(github.labelCreates.length, 0);
  });
});

test("unreachable or noncanonical revisions are blocked", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    github.reachableRevisions.clear();
    const unreachable = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(unreachable.overall_status, "repository_blocked");

    const wrongSource = validInput();
    wrongSource.vccp.source = "someone-else/control-plane";
    const noncanonical = await createOnboardingPlan({ repo: REPO, root, input: wrongSource, github });
    assert.equal(noncanonical.overall_status, "repository_blocked");
  });
});

test("audit distinguishes compliant artifacts, drift, external setup, and label metadata warnings", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const first = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan: first, root, github });
    github.labels[0].color = "ffffff";
    github.labels[0].description = "custom wording";

    const compliant = await auditConsumer({ repo: REPO, root, github });
    assert.equal(compliant.overall_status, "repository_ready");
    assert.ok(compliant.external_steps.length >= 2);
    assert.ok(compliant.warnings.some((warning) => warning.includes("label metadata")));

    await writeFile(path.join(root, ".github/vccp/reviewer-task-prompt.md"), "drift\n");
    const drift = await auditConsumer({ repo: REPO, root, github });
    assert.equal(drift.overall_status, "repository_blocked");
    assert.ok(drift.items.some((item) => item.status === "conflict"));
  });
});

test("reviewer manifest rejects unsupported check-completion task events", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const input = validInput();
    input.reviewer.events = ["pull_request.opened", "check_run.completed"];
    const plan = await createOnboardingPlan({ repo: REPO, root, input, github });
    assert.equal(plan.overall_status, "repository_blocked");
  });
});

test("rendered Reviewer prompt waits for same-run checks and enables Auto-merge before approval", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const prompt = await readFile(path.join(root, ".github/vccp/reviewer-task-prompt.md"), "utf8");
    assert.match(prompt, /same event-triggered run/);
    assert.match(prompt, /terminal/);
    assert.match(prompt, /If a required check has not appeared yet, treat it as not-yet-terminal and continue waiting/);
    assert.match(prompt, /remains absent until this run's reasonable waiting deadline or the platform execution limit[\s\S]*report `missing\/timeout`[\s\S]*exit without formal approval/);
    assert.match(prompt, /After every wait and state re-read, verify the PR's current head SHA is still the captured SHA/);
    assert.match(prompt, /enable native Auto-merge[\s\S]*APPROVE/);
    assert.match(prompt, /Dispatcher alone admits or rejects repairs under the shared `max_automated_repairs` budget/);
    assert.match(prompt, /If required CI is red, exit without a product `REQUEST_CHANGES`/);
    assert.doesNotMatch(prompt, /Count formal `REQUEST_CHANGES` reviews/);
    assert.doesNotMatch(prompt, /After the third formal request/);
    assert.doesNotMatch(prompt, /check_run\.completed/);
    assert.match(prompt, /never direct merge/i);
  });
});

test("CLI emits machine-readable plan JSON and routes all GitHub calls through the injected adapter", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const inputPath = path.join(root, "onboarding.json");
    await writeFile(inputPath, JSON.stringify(validInput()));
    let output = "";
    let errors = "";
    const code = await runOnboardingCli(["plan", "--repo", REPO, "--path", root, "--input", inputPath], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => { errors += text; } },
      githubFactory: () => github,
      runtimeRevision: REV_A,
    });
    assert.equal(code, 0);
    assert.equal(errors, "");
    const result = JSON.parse(output);
    assert.equal(result.command, "plan");
    assert.equal(result.repo, REPO);
    assert.equal(result.overall_status, "repository_changes_planned");
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));
  });
});

test("readiness CLI blocks an unavailable App credential without exposing secrets or touching SQLite", async () => {
  await withRepo(async (root) => {
    const consumerDir = path.join(root, ".github");
    await mkdir(consumerDir, { recursive: true });
    await writeFile(path.join(consumerDir, "control-plane.yml"), stringifyManifest(createManifest(validInput())));
    const stateDir = path.join(root, "local-state");
    const toolsDir = path.join(root, "tools");
    await mkdir(stateDir);
    await mkdir(toolsDir);
    const executable = path.join(toolsDir, "language_server.exe");
    await writeFile(executable, "fake");
    const configPath = path.join(root, "runtime-config.json");
    const databasePath = path.join(stateDir, "runtime.sqlite");
    await writeFile(configPath, JSON.stringify({
      database_path: databasePath,
      workspace: root,
      owner_id: "test-owner",
      antigravity_executable: executable,
      github_app: {
        app_id: "123456",
        credential_source: { type: "windows_dpapi_file", path: path.join(root, "missing.dpapi") },
      },
    }));
    let output = "";
    const code = await runOnboardingCli(["readiness", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: () => assert.fail("readiness should emit no stderr") },
      runtimeRevision: REV_A,
    });
    assert.equal(code, 2);
    const report = JSON.parse(output);
    assert.equal(report.overall_status, "machine_activation_blocked");
    assert.ok(report.runtime_activation.blockers.some((item) => item.includes("credential source is unavailable")));
    await assert.rejects(readFile(databasePath));
  });
});

test("CLI rejects a runtime revision mismatch before APPLY mutates files or GitHub", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    const planPath = path.join(root, "plan.json");
    await writeFile(planPath, JSON.stringify(plan));
    let errors = "";
    const code = await runOnboardingCli(["apply", "--repo", REPO, "--path", root, "--plan", planPath], {
      stdout: { write: () => {} },
      stderr: { write: (text) => { errors += text; } },
      githubFactory: () => github,
      runtimeRevision: REV_B,
    });
    assert.equal(code, 3);
    assert.equal(JSON.parse(errors).overall_status, "command_failed");
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));
  });
});
