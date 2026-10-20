import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { createOnboardingPlan } from "../../lib/onboarding/plan.mjs";
import { applyOnboardingPlan, auditConsumer } from "../../lib/onboarding/apply.mjs";
import { fastForwardMergedConsumer, runOnboardingCli } from "../../lib/onboarding/cli.mjs";
import { createManifest, parseManifest, stringifyManifest, validateManifest } from "../../lib/onboarding/manifest.mjs";

const SOURCE = "liuchangchxy/vibe-coding-control-plane";
const REV_A = "a".repeat(40);
const REV_B = "b".repeat(40);
const REV_C = "c".repeat(40);
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
  async getBranch(repo, name) {
    if (repo === SOURCE && name === "main") return { name, commit: { sha: REV_B } };
    return repo === REPO && this.repository.branches.includes(name)
      ? { name, commit: { sha: "c".repeat(40) } } : null;
  }
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

async function writeCleanupWorkflow(root, workflowName, scriptName, script) {
  const workflowDir = path.join(root, ".github/workflows");
  const scriptDir = path.join(root, ".github/scripts");
  await mkdir(workflowDir, { recursive: true });
  await mkdir(scriptDir, { recursive: true });
  await writeFile(path.join(workflowDir, workflowName), `on:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/${scriptName}\n`);
  await writeFile(path.join(scriptDir, scriptName), script);
}

function localEnrollment(root) {
  return {
    database_path: path.join(root, "local-state", "runtime.sqlite"),
    workspace: root,
    target_repository: REPO,
    enrollment: { authorized_repository: REPO },
    owner_id: "test-owner",
    antigravity_executable: path.join(root, "language_server.exe"),
  };
}

async function readinessReport(root, github, localConfig = localEnrollment(root)) {
  const configPath = path.join(root, "runtime-config.json");
  await writeFile(configPath, JSON.stringify(localConfig));
  let output = "";
  const code = await runOnboardingCli(["readiness", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
    stdout: { write: (text) => { output += text; } },
    stderr: { write: (text) => assert.fail(text) },
    githubFactory: () => github,
    runtimeQualifier: async () => ({ runtime_activation: { status: "ready", blockers: [] } }),
    runtimeRevision: REV_A,
    hostInspector: () => ({ status: "not_installed" }),
  });
  return { code, report: JSON.parse(output) };
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
    assert.equal(plan.machine_authorization.status, "unknown");
    assert.equal(plan.machine_activation.status, "unknown");
    assert.equal(plan.repository_status, "changes_planned");
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));
  });
});

test("deploy checkout convergence fast-forwards only a clean base branch", async () => {
  const parent = await mkdtemp(path.join(tmpdir(), "vccp-fast-forward-"));
  try {
    const remote = path.join(parent, "remote.git");
    const consumer = path.join(parent, "consumer");
    const updater = path.join(parent, "updater");
    execFileSync("git", ["init", "--bare", "-q", remote]);
    execFileSync("git", ["init", "-q", "-b", "main", consumer]);
    execFileSync("git", ["-C", consumer, "config", "user.email", "test@example.com"]);
    execFileSync("git", ["-C", consumer, "config", "user.name", "Test"]);
    await writeFile(path.join(consumer, "README.md"), "base\n");
    execFileSync("git", ["-C", consumer, "add", "."]);
    execFileSync("git", ["-C", consumer, "commit", "-qm", "base"]);
    execFileSync("git", ["-C", consumer, "remote", "add", "origin", remote]);
    execFileSync("git", ["-C", consumer, "push", "-qu", "origin", "main"]);
    execFileSync("git", ["clone", "-q", "--branch", "main", remote, updater]);
    execFileSync("git", ["-C", updater, "config", "user.email", "test@example.com"]);
    execFileSync("git", ["-C", updater, "config", "user.name", "Test"]);
    await writeFile(path.join(updater, "README.md"), "merged\n");
    execFileSync("git", ["-C", updater, "commit", "-qam", "merged maintenance PR"]);
    execFileSync("git", ["-C", updater, "push", "-q", "origin", "HEAD:main"]);
    assert.equal(fastForwardMergedConsumer(consumer, "main"), true);
    assert.equal((await readFile(path.join(consumer, "README.md"), "utf8")).replace(/\r\n/g, "\n"), "merged\n");
    await writeFile(path.join(consumer, "README.md"), "local edits\n");
    await writeFile(path.join(updater, "README.md"), "next merge\n");
    execFileSync("git", ["-C", updater, "commit", "-qam", "next merge"]);
    execFileSync("git", ["-C", updater, "push", "-q", "origin", "HEAD:main"]);
    assert.equal(fastForwardMergedConsumer(consumer, "main"), false);
    assert.equal(await readFile(path.join(consumer, "README.md"), "utf8"), "local edits\n");
  } finally { await rm(parent, { recursive: true, force: true }); }
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

test("consumer-owned equivalent cleanup satisfies the capability without a VCCP adapter", async () => {
  await withRepo(async (root) => {
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    await writeFile(path.join(workflowDir, "close-housekeeping.yml"), "on:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/prune-coordination.js\n");
    await writeFile(path.join(scriptDir, "prune-coordination.js"), "const ACTIVE_LABELS = ['agent-ready', 'agent-working', 'changes-requested'];\nconst TERMINAL_LABELS = ['infra-blocked', 'needs-human'];\nfor (const label of ACTIVE_LABELS) await removeLabel(label);\n");
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github: new FakeGitHub() });
    assert.equal(plan.capabilities["coordination-label-cleanup"].status, "satisfied");
    assert.equal(plan.capabilities["coordination-label-cleanup"].provider, "consumer");
    assert.equal(plan.items.some((entry) => entry.path === ".github/workflows/vccp-coordination-label-cleanup.yml"), false);
  });
});

test("VCCP cleanup does not block or modify an existing issues.closed workflow", async () => {
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
    assert.equal(plan.overall_status, "repository_changes_planned");
    const conflict = plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup");
    assert.equal(conflict.status, "satisfied");
    assert.equal(conflict.provider, "consumer");

    const applied = await applyOnboardingPlan({ plan, root, github });
    assert.equal(applied.overall_status, "repository_ready");
    assert.deepEqual(await readFile(path.join(workflowDir, "coordination-label-cleanup.yml"), "utf8"), cleanupWorkflow);

    await mkdir(path.join(root, ".github"), { recursive: true });
    await writeFile(path.join(root, ".github/control-plane.yml"), stringifyManifest(createManifest(validInput())));
    const audit = await auditConsumer({ repo: REPO, root, github });
    assert.equal(audit.overall_status, "repository_ready");
    assert.ok(audit.items.some((entry) => entry.id === "capability:coordination-label-cleanup" && entry.status === "satisfied"));
  });
});

test("issues.closed cleanup with repository-relative script is preserved and accepted", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    await writeFile(path.join(workflowDir, "coordination-label-cleanup.yml"), `name: Coordination Label Cleanup\non:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/cleanup-coordination-labels.js\n`);
    await writeFile(path.join(scriptDir, "cleanup-coordination-labels.js"), `const ACTIVE_LABELS = ["agent-ready", "agent-working", "changes-requested"];\nconst TERMINAL_LABELS = ["infra-blocked", "needs-human"];\nasync function cleanup(issue, removeLabel) {\n  const labels = new Set(issue.labels.map(({ name }) => name));\n  if (TERMINAL_LABELS.some((name) => labels.has(name))) return { preservedTerminalState: true, removed: [] };\n  for (const name of ACTIVE_LABELS) if (labels.has(name)) await removeLabel(name);\n}\n`);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_changes_planned");
    assert.equal(plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup").status, "satisfied");
  });
});

test("unresolved third-party cleanup script does not block installation", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    await mkdir(workflowDir, { recursive: true });
    await writeFile(path.join(workflowDir, "coordination-label-cleanup.yml"), `name: Coordination Label Cleanup\non:\n  issues:\n    types: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node .github/scripts/cleanup-coordination-labels\n`);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_changes_planned");
    const conflict = plan.items.find((entry) => entry.id === "capability:coordination-label-cleanup");
    assert.equal(conflict.status, "satisfied");
  });
});

test("third-party extensionless cleanup script is accepted without semantic analysis", async () => {
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
    assert.equal(plan.overall_status, "repository_changes_planned");
  });
});

test("an overlapping cleanup with unproven behavior does not block installation", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const workflowDir = path.join(root, ".github/workflows");
    const scriptDir = path.join(root, ".github/scripts");
    await mkdir(workflowDir, { recursive: true });
    await mkdir(scriptDir, { recursive: true });
    await writeFile(path.join(workflowDir, "issue-close.yml"), `on:\n  issues: [closed]\njobs:\n  cleanup:\n    steps:\n      - run: node \"\${process.env.GITHUB_WORKSPACE}/.github/scripts/cleanup.js\"\n`);
    await writeFile(path.join(scriptDir, "cleanup.js"), `const ACTIVE_LABELS = ["agent-ready"];\nawait github.rest.issues.removeLabel({ name: ACTIVE_LABELS[0] });\n`);

    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    assert.equal(plan.overall_status, "repository_changes_planned");
  });
});

test("multiple Issue-close workflows are retained without semantic conflict guesses", async () => {
  await withRepo(async (root) => {
    const compatible = "const ACTIVE_LABELS = ['agent-ready', 'agent-working', 'changes-requested'];\nconst TERMINAL_LABELS = ['infra-blocked', 'needs-human'];\nfor (const label of ACTIVE_LABELS) await removeLabel(label);\n";
    const conflict = "const ACTIVE_LABELS = ['agent-ready', 'agent-working', 'changes-requested'];\nconst TERMINAL_LABELS = ['infra-blocked', 'needs-human'];\nif (TERMINAL_LABELS.some((label) => labels.has(label))) return { removed: [] };\nfor (const label of ACTIVE_LABELS) await removeLabel(label);\n";
    await writeCleanupWorkflow(root, "z-conflict.yml", "conflict.js", conflict);
    await writeCleanupWorkflow(root, "a-compatible.yml", "compatible.js", compatible);
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github: new FakeGitHub() });
    assert.equal(plan.capabilities["coordination-label-cleanup"].status, "satisfied");
  });
});

test("Issue-close workflow sorting does not change cleanup ownership", async () => {
  await withRepo(async (root) => {
    const compatible = "const ACTIVE_LABELS = ['agent-ready', 'agent-working', 'changes-requested'];\nconst TERMINAL_LABELS = ['infra-blocked', 'needs-human'];\nfor (const label of ACTIVE_LABELS) await removeLabel(label);\n";
    const conflict = "const ACTIVE_LABELS = ['agent-ready', 'agent-working', 'changes-requested'];\nconst TERMINAL_LABELS = ['infra-blocked', 'needs-human'];\nif (TERMINAL_LABELS.some((label) => labels.has(label))) return { removed: [] };\nfor (const label of ACTIVE_LABELS) await removeLabel(label);\n";
    await writeCleanupWorkflow(root, "z-compatible.yml", "compatible.js", compatible);
    await writeCleanupWorkflow(root, "a-conflict.yml", "conflict.js", conflict);
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github: new FakeGitHub() });
    assert.equal(plan.capabilities["coordination-label-cleanup"].status, "satisfied");
  });
});

test("third-party Issue-close label scripts are not statically analyzed", async () => {
  await withRepo(async (root) => {
    await writeCleanupWorkflow(root, "close-reopen.yml", "reopen.js",
      'await addLabel("agent-working");\n');
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github: new FakeGitHub() });
    const capability = plan.capabilities["coordination-label-cleanup"];
    assert.equal(capability.status, "satisfied");
    assert.equal(capability.provider, "consumer");
  });
});

test("v2 manifest contract retains the compatible repair-budget field", () => {
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
  assert.match(onboarding, /Python owns activation qualification and construction of the Generic Runtime, plus claim\/ownership, discovery, dispatch, repair admission/);
  assert.match(contract, /independent caps of three implementation-attributable CI repairs and three formal Reviewer repairs, plus a hard total of five/);
  assert.match(contract, /Dispatcher admits a candidate/);
  assert.match(contract, /repair:<repo>:pr:<pr_number>:head:<failed_or_rejected_head_sha>/);
  assert.match(contract, /same linked PR and implementation branch/);
  assert.match(contract, /schema v1 is unsupported/);
  assert.match(reviewer, /Dispatcher alone admits or rejects repairs/);
  assert.match(reviewer, /VCCP deploy revision-upgrade PR/);
  assert.match(reviewer, /full 64-hex PLAN id/);
  assert.match(reviewer, /reviewer-task-prompt\.md/);
  assert.match(reviewer, /A verified VCCP maintenance PR has no Issue/);
  assert.match(reviewer, /Do not use it for remove PRs, onboarding PRs/);
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

    const deployPlan = await createOnboardingPlan({ repo: REPO, root, github, upgradeLegacy: true });
    assert.equal(deployPlan.overall_status, "repository_changes_planned",
      JSON.stringify(deployPlan.items.filter(({ status }) => ["blocked", "conflict"].includes(status))));
    assert.equal(deployPlan.desired_manifest.schema_version, 2);
    assert.ok(deployPlan.items.some(({ id, path: file, status }) => id === "manifest" && file && status === "update"));
    assert.ok(deployPlan.items.some(({ id, status }) => id !== "manifest" && ["create", "update"].includes(status)));

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

test("deploy turns a valid schema v1 Consumer into a deterministic v2 maintenance upgrade", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    github.repository.branches.push("release");
    const source = `schema_version: 1\nvccp:\n  source: ${SOURCE}\n  revision: ${REV_A}\nrepository:\n  base_branch: release\n  implementer_authors:\n    - custom-builder[bot]\nissue_contract:\n  frozen_spec_label: frozen-spec\n  active_coordination_labels: [agent-ready, agent-working, changes-requested]\n  terminal_coordination_labels: [infra-blocked, needs-human]\n  max_request_changes_rounds: 3\nreviewer:\n  transport: chatgpt-work-github-webhook-task\n  events: [pull_request.opened, pull_request.ready_for_review, pull_request.synchronize]\n  exact_head_sha_required: true\n  required_checks_completion: same_run_wait_until_terminal\n  required_checks:\n    - name: Custom CI\n      accepted_conclusions: [success]\nmerge:\n  native_auto_merge: true\n  enable_native_auto_merge_by: reviewer\n  method: rebase\n  direct_merge_forbidden: true\n  admin_bypass_forbidden_in_normal_flow: true\nreporting:\n  pull_request_template: true\n`;
    await mkdir(path.join(root, ".github"), { recursive: true });
    await writeFile(path.join(root, ".github/control-plane.yml"), source);
    const configPath = path.join(root, "runtime.json");
    await writeFile(configPath, JSON.stringify(localEnrollment(root)));
    let published;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github,
      maintenancePublisher: async ({ plan }) => { published = plan; return { status: "open", number: 17 }; },
    });
    const report = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(report.overall_status, "MAINTENANCE_PR_OPEN",
      JSON.stringify(report.items?.filter(({ status }) => ["blocked", "conflict"].includes(status)) ?? report.errors));
    assert.equal(published.overall_status, "repository_changes_planned");
    assert.equal(published.desired_manifest.schema_version, 2);
    assert.equal(published.desired_manifest.repository.base_branch, "release");
    assert.deepEqual(published.desired_manifest.repository.implementer_authors, ["custom-builder[bot]"]);
    assert.deepEqual(published.desired_manifest.reviewer.required_checks, [{ name: "Custom CI", accepted_conclusions: ["success"] }]);
    assert.equal(published.desired_manifest.merge.method, "rebase");
    assert.equal(published.desired_manifest.reporting.pull_request_template, true);
    assert.equal(published.items.find(({ id }) => id === "manifest").status, "update");
    assert.match(published.items.find(({ id }) => id === "manifest").content, /schema_version: 2/);
    assert.equal(await readFile(path.join(root, ".github/control-plane.yml"), "utf8"), source);
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

test("managed text audit canonicalizes Windows newlines while retaining raw stale-plan fencing", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const initial = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan: initial, root, github });

    const lock = JSON.parse(await readFile(path.join(root, ".github/vccp/installation-lock.json"), "utf8"));
    const managedPaths = [
      ".github/workflows/vccp-coordination-label-cleanup.yml",
      ".github/vccp/reviewer-task-prompt.md",
    ];
    const original = new Map();
    for (const relativePath of managedPaths) {
      const target = path.join(root, relativePath);
      const lfContent = await readFile(target, "utf8");
      original.set(relativePath, lfContent);
      const hash = createHash("sha256").update(lfContent.replace(/\r\n/g, "\n")).digest("hex");
      assert.equal(lock.artifacts.find((record) => record.path === relativePath).sha256, hash);
      await writeFile(target, lfContent.replace(/\r?\n/g, "\r\n"));
    }

    const windowsAudit = await auditConsumer({ repo: REPO, root, github });
    assert.equal(windowsAudit.overall_status, "repository_ready");
    assert.equal(windowsAudit.items.find((entry) => entry.id === "file:.github/workflows/vccp-coordination-label-cleanup.yml").status, "satisfied");
    assert.equal(windowsAudit.items.find((entry) => entry.id === "file:.github/vccp/reviewer-task-prompt.md").status, "satisfied");

    const reviewerPath = ".github/vccp/reviewer-task-prompt.md";
    const reviewerFile = path.join(root, reviewerPath);
    await writeFile(reviewerFile, `${await readFile(reviewerFile, "utf8")}Changed body.\r\n`);
    const editedAudit = await auditConsumer({ repo: REPO, root, github });
    assert.equal(editedAudit.overall_status, "repository_blocked");
    assert.equal(editedAudit.items.find((entry) => entry.id === `file:${reviewerPath}`).status, "conflict");

    await writeFile(reviewerFile, original.get(reviewerPath).replace(/\n/g, "\r\n"));
    const planned = await createOnboardingPlan({ repo: REPO, root, github });
    assert.equal(planned.overall_status, "repository_ready");
    await writeFile(reviewerFile, original.get(reviewerPath));
    const stale = await applyOnboardingPlan({ plan: planned, root, github });
    assert.equal(stale.overall_status, "stale_plan");
    assert.equal(stale.exit_code, 4);
    assert.equal(github.labelCreates.length, 6);
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
    assert.match(prompt, /Dispatcher alone admits or rejects repairs under its independent CI and Reviewer caps and hard total budget/);
    assert.match(prompt, /If required CI is red, exit without a product `REQUEST_CHANGES`/);
    assert.match(prompt, /No first-finding early exit/);
    assert.match(prompt, /Root-cause-wide sibling audit/);
    assert.match(prompt, /Cumulative review/);
    assert.match(prompt, /Complete actionable blocking list/);
    assert.match(prompt, /No scope inflation/);
    assert.match(prompt, /Evidence types/);
    assert.match(prompt, /listed locations are confirmed examples, not an exhaustive patch list/);
    assert.match(prompt, /next review to repeat the Frozen-Spec-wide audit/);
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
      githubFactory: () => new FakeGitHub(),
      runtimeRevision: REV_A,
    });
    assert.equal(code, 2);
    const report = JSON.parse(output);
    assert.equal(report.overall_status, "machine_activation_blocked");
    assert.equal(report.repository_status, "changes_planned");
    assert.equal(report.machine_authorization.status, "required");
    assert.ok(report.runtime_activation.blockers.some((item) => item.includes("credential source is unavailable")));
    await assert.rejects(readFile(databasePath));
  });
});

test("readiness does not block on guessed third-party cleanup semantics", async () => {
  await withRepo(async (root) => {
    const controlPlane = path.join(root, ".github");
    await mkdir(controlPlane, { recursive: true });
    await writeFile(path.join(controlPlane, "control-plane.yml"), stringifyManifest(createManifest(validInput())));
    await writeCleanupWorkflow(root, "close-cleanup.yml", "cleanup.js",
      "const ACTIVE_LABELS = ['agent-ready', 'agent-working', 'changes-requested'];\nconst TERMINAL_LABELS = ['infra-blocked', 'needs-human'];\nif (TERMINAL_LABELS.some((label) => labels.has(label))) return { removed: [] };\nfor (const label of ACTIVE_LABELS) await removeLabel(label);\n");
    const github = new FakeGitHub();
    const { code, report } = await readinessReport(root, github);
    assert.equal(code, 2);
    assert.equal(report.repository_status, "changes_planned");
    assert.equal(report.repository_audit.capabilities["coordination-label-cleanup"].status, "satisfied");
    assert.equal(report.machine_authorization.status, "authorized");
    assert.equal(report.machine_activation.status, "ready");
    assert.equal(report.overall_status, "machine_activation_blocked");
    assert.equal(github.labelCreates.length, 0);
  });
});

test("readiness does not promote a repository with onboarding changes to ready", async () => {
  await withRepo(async (root) => {
    const controlPlane = path.join(root, ".github");
    await mkdir(controlPlane, { recursive: true });
    await writeFile(path.join(controlPlane, "control-plane.yml"), stringifyManifest(createManifest(validInput())));
    const github = new FakeGitHub();
    const { code, report } = await readinessReport(root, github);
    assert.equal(code, 2);
    assert.equal(report.repository_status, "changes_planned");
    assert.equal(report.machine_activation.status, "ready");
    assert.equal(report.overall_status, "machine_activation_blocked");
    assert.equal(github.labelCreates.length, 0);
  });
});

test("readiness succeeds when canonical repository audit and machine qualification are ready", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const { code, report } = await readinessReport(root, github);
    assert.equal(code, 0);
    assert.equal(report.repository_status, "ready");
    assert.equal(report.machine_authorization.status, "authorized");
    assert.equal(report.machine_activation.status, "ready");
    assert.equal(report.overall_status, "machine_activation_ready");
  });
});

test("status reports NEEDS_ACTION until the qualified Runner is actually running", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime-config.json");
    await writeFile(configPath, JSON.stringify(localEnrollment(root)));
    let output = "";
    const code = await runOnboardingCli(["status", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github,
      runtimeQualifier: async () => ({ runtime_activation: { status: "ready", blockers: [] } }),
      runtimeRevision: REV_A,
      hostInspector: () => ({ mechanism: "windows_task_scheduler", status: "stopped" }),
    });
    const report = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(report.command, "status");
    assert.equal(report.overall_status, "NEEDS_ACTION");
    assert.equal(report.repository_status, "ready");
    assert.equal(report.machine_activation.status, "ready");
    assert.equal(report.desired_revision, REV_A);
    assert.equal(report.installation_lock.status, "satisfied");
    assert.equal(report.machine_runtime.implementer_provider, null);
    assert.ok(["launchd", "unsupported", "windows_task_scheduler"].includes(report.host_runner.mechanism));
  });
});

test("status recognizes an uninstalled Consumer as NEEDS_ACTION", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const configPath = path.join(root, "runtime-config.json");
    await writeFile(configPath, JSON.stringify(localEnrollment(root)));
    let output = "";
    const code = await runOnboardingCli(["status", "--repo", REPO, "--path", root,
      "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, hostInspector: () => ({ status: "not_installed" }),
    });
    assert.equal(code, 2);
    const report = JSON.parse(output);
    assert.equal(report.desired_revision, null);
    assert.equal(report.repository_status, "not_installed");
    assert.equal(report.repository_audit.overall_status, "not_installed");
    assert.equal(report.host_runner.status, "not_installed");
    assert.equal(report.overall_status, "NEEDS_ACTION");
  });
});

test("status trusts only an exact running clean host and avoids a lock-conflicting readiness probe", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime-config.json");
    await writeFile(configPath, JSON.stringify({ ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_agent_sdk", type: "claude_agent_sdk" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_agent_sdk",
      reviewer_github_identity: "reviewer-user" }));
    let output = "";
    const code = await runOnboardingCli(["status", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, runtimeRevision: REV_A,
      hostInspector: () => ({ status: "running", running_revision: REV_A, running_checkout_clean: true,
        config_matches: true, pid: 1234 }),
      runtimeQualifier: async () => assert.fail("status must not contend with the running RuntimeLock"),
    });
    const report = JSON.parse(output);
    assert.equal(code, 0);
    assert.equal(report.overall_status, "READY");
    assert.equal(report.host_runner.pid, 1234);
    assert.equal(report.machine_activation.source, "verified_running_runtime");
    assert.equal(report.machine_runtime.reviewer_provider, "claude_agent_sdk");
  });
});

test("status blocks a running Consumer owner on a different revision", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime-config.json");
    await writeFile(configPath, JSON.stringify(localEnrollment(root)));
    let output = "";
    const code = await runOnboardingCli(["status", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, runtimeRevision: REV_A,
      hostInspector: () => ({ status: "running", running_revision: REV_B,
        running_checkout_clean: true, config_matches: false, pid: 1234 }),
    });
    assert.equal(code, 2);
    assert.equal(JSON.parse(output).overall_status, "BLOCKED");
  });
});

test("status blocks a running exact-revision host whose config digest drifted", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime-config.json");
    await writeFile(configPath, JSON.stringify(localEnrollment(root)));
    let output = "";
    const code = await runOnboardingCli(["status", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, runtimeRevision: REV_A,
      hostInspector: () => ({ status: "running", running_revision: REV_A,
        running_checkout_clean: true, config_matches: false, pid: 1234 }),
      runtimeQualifier: async () => ({ runtime_activation: { status: "ready", blockers: [] } }),
    });
    assert.equal(code, 2);
    assert.equal(JSON.parse(output).overall_status, "BLOCKED");
  });
});

test("deploy reuses PLAN and opens a structured maintenance PR without changing the base checkout", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    await writeFile(path.join(root, "input.json"), JSON.stringify(validInput()));
    await writeFile(path.join(root, "runtime.json"), JSON.stringify(localEnrollment(root)));
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root, "--input", path.join(root, "input.json"), "--runtime-config", path.join(root, "runtime.json")], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github,
      maintenancePublisher: async () => ({ status: "open", number: 7, url: "https://github.com/example/consumer/pull/7" }),
      runtimeRevision: REV_A,
    });
    const report = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(report.command, "deploy");
    assert.equal(report.repository_status, "changes_planned");
    assert.equal(report.overall_status, "MAINTENANCE_PR_OPEN");
    assert.equal(report.next_action.type, "maintenance_pr_review_and_merge");
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));
  });
});

test("deploy requires explicit first-time provider bindings and does not mutate machine config", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const initial = { ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ] };
    await writeFile(configPath, JSON.stringify(initial));
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_A], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A,
      hostDeployer: async () => assert.fail("must not start a guessed provider"),
    });
    const report = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(report.next_action.type, "bind_local_agent_providers");
    assert.match(report.next_action.detail, /explicitly select.*implementer.*reviewer/i);
    assert.deepEqual(JSON.parse(await readFile(configPath, "utf8")), initial);
  });
});

test("deploy binds explicitly selected providers once then delegates the ready repository to host reconciliation", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const config = { ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ] };
    await writeFile(configPath, JSON.stringify(config));
    let deployed;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_A,
      "--implementer-provider", "antigravity", "--reviewer-provider", "claude_code",
      "--reviewer-identity", "reviewer-user"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A, hostDeployer: async (request) => { deployed = request; return { status: "running", no_op: true, revision: REV_A }; },
    });
    const report = JSON.parse(output);
    const written = JSON.parse(await readFile(configPath, "utf8"));
    assert.equal(code, 0);
    assert.equal(report.overall_status, "RUNNING");
    assert.equal(written.implementer_provider, "antigravity");
    assert.equal(written.reviewer_provider, "claude_code");
    assert.equal(written.reviewer_github_identity, "reviewer-user");
    assert.equal(deployed.manifest.vccp.revision, REV_A);
  });
});

test("deploy keeps an installed manifest pin unless a revision is explicitly proposed", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(REV_A), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    await writeFile(configPath, JSON.stringify({ ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_code",
      reviewer_github_identity: "reviewer-user" }));
    let deployed;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A,
      hostDeployer: async (request) => { deployed = request; return { status: "running", revision: REV_A }; },
    });
    assert.equal(code, 0);
    assert.equal(JSON.parse(output).desired_revision, REV_A);
    assert.equal(deployed.manifest.vccp.revision, REV_A);
  });
});

test("deploy persists an open upgrade target, resumes it after merge, and clears it after runtime convergence", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const initial = await createOnboardingPlan({ repo: REPO, root, input: validInput(REV_A), github });
    await applyOnboardingPlan({ plan: initial, root, github });
    const configPath = path.join(root, "runtime.json");
    await writeFile(configPath, JSON.stringify({ ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_code", reviewer_github_identity: "reviewer-user" }));

    let output = "";
    const firstCode = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_B], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, maintenancePublisher: async ({ plan }) => ({ status: "open", plan_id: plan.plan_id }),
    });
    assert.equal(firstCode, 2);
    assert.deepEqual(JSON.parse(await readFile(configPath, "utf8")).upgrade_target,
      { repository: REPO, revision: REV_B, from_revision: REV_A });

    output = "";
    const rerunCode = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, maintenancePublisher: async ({ plan }) => {
        assert.equal(plan.desired_manifest.vccp.revision, REV_B);
        return { status: "open", plan_id: plan.plan_id };
      },
    });
    assert.equal(rerunCode, 2);

    const mergedUpgrade = await createOnboardingPlan({ repo: REPO, root, github, revisionProposal: REV_B });
    await applyOnboardingPlan({ plan: mergedUpgrade, root, github });
    github.reachableRevisions.add(REV_C);
    const getBranch = github.getBranch.bind(github);
    github.getBranch = async (repo, name) => repo === SOURCE && name === "main"
      ? { name, commit: { sha: REV_C } } : getBranch(repo, name);
    let deployed;
    output = "";
    const resumeCode = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, runtimeRevision: REV_B,
      hostDeployer: async (request) => { deployed = request; return { status: "running", revision: REV_B }; },
    });
    assert.equal(resumeCode, 0, output);
    assert.equal(deployed.manifest.vccp.revision, REV_B);
    assert.equal(JSON.parse(await readFile(configPath, "utf8")).upgrade_target, undefined);
  });
});

test("an upgrade rerun keeps its merged target when canonical main has advanced again", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    github.reachableRevisions.add(REV_C);
    const initial = await createOnboardingPlan({ repo: REPO, root, input: validInput(REV_A), github });
    await applyOnboardingPlan({ plan: initial, root, github });
    const upgrade = await createOnboardingPlan({ repo: REPO, root, github, revisionProposal: REV_B });
    await applyOnboardingPlan({ plan: upgrade, root, github });
    const originalGetBranch = github.getBranch.bind(github);
    github.getBranch = async (repo, name) => repo === SOURCE && name === "main"
      ? { name, commit: { sha: REV_C } } : originalGetBranch(repo, name);
    const configPath = path.join(root, "runtime.json");
    await writeFile(configPath, JSON.stringify({ ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_code",
      reviewer_github_identity: "reviewer-user" }));
    let deployed;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_B,
      hostDeployer: async (request) => { deployed = request; return { status: "running", revision: REV_B }; },
    });
    assert.equal(code, 0, output);
    assert.equal(JSON.parse(output).desired_revision, REV_B);
    assert.equal(deployed.manifest.vccp.revision, REV_B);
  });
});

test("deploy allows an explicit provider rebind when no Consumer execution is active", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const config = { ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
      { key: "new-implementer", type: "antigravity", executable: "/bin/true" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_code",
      reviewer_github_identity: "reviewer-user" };
    await writeFile(configPath, JSON.stringify(config));
    let deployed;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--implementer-provider", "new-implementer"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A, providerRebindChecker: () => ({ status: "ready" }),
      hostDeployer: async (request) => { deployed = request; return { status: "running", revision: REV_A }; },
    });
    assert.equal(code, 0, output);
    assert.equal(JSON.parse(await readFile(configPath, "utf8")).implementer_provider, "new-implementer");
    assert.equal(deployed.localConfig.implementer_provider, "new-implementer");
  });
});

test("deploy preserves provider bindings while a Consumer execution is active", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const config = { ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
      { key: "new-implementer", type: "antigravity", executable: "/bin/true" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_code",
      reviewer_github_identity: "reviewer-user" };
    await writeFile(configPath, JSON.stringify(config));
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--implementer-provider", "new-implementer"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      providerRebindChecker: () => ({ status: "blocked", reason: "active Consumer execution exists (LAUNCH_CONFIRMED)" }),
      hostDeployer: async () => assert.fail("must preserve the active provider binding"),
    });
    assert.equal(code, 2);
    assert.equal(JSON.parse(output).next_action.type, "bind_local_agent_providers");
    assert.deepEqual(JSON.parse(await readFile(configPath, "utf8")), config);
  });
});

test("deploy explicitly migrates an existing Claude Code Reviewer binding to Agent SDK", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const initial = { ...localEnrollment(root), providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ], implementer_provider: "antigravity", reviewer_provider: "claude_code",
      reviewer_github_identity: "reviewer-user" };
    await writeFile(configPath, JSON.stringify(initial));
    let deployed;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_A,
      "--reviewer-provider", "claude_agent_sdk"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A,
      hostDeployer: async (request) => {
        deployed = request;
        return { status: "running", revision: REV_A };
      },
    });
    const report = JSON.parse(output);
    const written = JSON.parse(await readFile(configPath, "utf8"));
    assert.equal(code, 0);
    assert.equal(report.overall_status, "RUNNING");
    assert.equal(written.implementer_provider, "antigravity");
    assert.equal(written.reviewer_provider, "claude_agent_sdk");
    assert.equal(written.reviewer_github_identity, "reviewer-user");
    assert.ok(written.providers.some((provider) => provider.key === "claude_code"));
    assert.ok(written.providers.some((provider) => provider.key === "claude_agent_sdk"
      && provider.type === "claude_agent_sdk"));
    assert.equal(deployed.localConfig.reviewer_provider, "claude_agent_sdk");
  });
});

test("deploy requires explicit repository enrollment and saves it with the provider binding", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const config = { ...localEnrollment(root), enrollment: {}, providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ] };
    await writeFile(configPath, JSON.stringify(config));
    let deployed;
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_A,
      "--authorize-repository", REPO,
      "--implementer-provider", "antigravity", "--reviewer-provider", "claude_code",
      "--reviewer-identity", "reviewer-user"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A,
      hostDeployer: async (request) => { deployed = request; return { status: "running", revision: REV_A }; },
    });
    const report = JSON.parse(output);
    const written = JSON.parse(await readFile(configPath, "utf8"));
    assert.equal(code, 0);
    assert.equal(report.overall_status, "RUNNING");
    assert.equal(written.enrollment.authorized_repository, REPO);
    assert.equal(written.implementer_provider, "antigravity");
    assert.equal(written.reviewer_provider, "claude_code");
    assert.equal(deployed.localConfig.enrollment.authorized_repository, REPO);
  });
});

test("deploy does not create machine enrollment without an explicit authorization", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const config = { ...localEnrollment(root), enrollment: {}, providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ] };
    await writeFile(configPath, JSON.stringify(config));
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_A,
      "--implementer-provider", "antigravity", "--reviewer-provider", "claude_code",
      "--reviewer-identity", "reviewer-user"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A,
      hostDeployer: async () => assert.fail("must not deploy without human enrollment authorization"),
    });
    const report = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(report.next_action.type, "authorize_machine_enrollment");
    assert.match(report.next_action.detail, /--authorize-repository example\/consumer/);
    assert.deepEqual(JSON.parse(await readFile(configPath, "utf8")), config);
  });
});

test("deploy refuses an enrollment authorization for a different repository", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const plan = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    await applyOnboardingPlan({ plan, root, github });
    const configPath = path.join(root, "runtime.json");
    const config = { ...localEnrollment(root), enrollment: {}, providers: [
      { key: "antigravity", type: "antigravity", executable: "/bin/true" },
      { key: "claude_code", type: "claude_code", executable: "/bin/true" },
    ] };
    await writeFile(configPath, JSON.stringify(config));
    let output = "";
    const code = await runOnboardingCli(["deploy", "--repo", REPO, "--path", root,
      "--runtime-config", configPath, "--propose-revision", REV_A,
      "--authorize-repository", "another/repository",
      "--implementer-provider", "antigravity", "--reviewer-provider", "claude_code",
      "--reviewer-identity", "reviewer-user"], {
      stdout: { write: (text) => { output += text; } },
      stderr: { write: (text) => assert.fail(text) }, githubFactory: () => github,
      runtimeRevision: REV_A,
      hostDeployer: async () => assert.fail("must not deploy a differently authorized repository"),
    });
    const report = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(report.next_action.type, "authorize_machine_enrollment");
    assert.match(report.next_action.detail, /must match the requested Consumer/);
    assert.deepEqual(JSON.parse(await readFile(configPath, "utf8")), config);
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
