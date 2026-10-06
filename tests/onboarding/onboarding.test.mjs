import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { createOnboardingPlan } from "../../lib/onboarding/plan.mjs";
import { applyOnboardingPlan, auditConsumer } from "../../lib/onboarding/apply.mjs";
import { runOnboardingCli } from "../../lib/onboarding/cli.mjs";

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
    assert.equal(plan.items.find((item) => item.id === "manifest").status, "create");
    assert.equal(plan.items.find((item) => item.id === "labels").status, "create");
    assert.equal(plan.external_steps.find((item) => item.id === "reviewer-task").status, "external-step");
    assert.equal(plan.external_steps.find((item) => item.id === "dispatcher").status, "external-step");
    assert.equal(github.labelCreates.length, 0);
    await assert.rejects(readFile(path.join(root, ".github/control-plane.yml")));
  });
});

test("apply writes the consumer state and the second plan/apply is a no-op", async () => {
  await withRepo(async (root) => {
    const github = new FakeGitHub();
    const first = await createOnboardingPlan({ repo: REPO, root, input: validInput(), github });
    const applied = await applyOnboardingPlan({ plan: first, root, github });
    assert.equal(applied.overall_status, "repository_ready_external_pending");
    assert.equal(applied.exit_code, 0);
    assert.equal(github.labelCreates.length, 6);

    const second = await createOnboardingPlan({ repo: REPO, root, github });
    assert.equal(second.overall_status, "repository_ready_external_pending");
    assert.ok(second.items.every((item) => item.status === "satisfied" || item.status === "external-step"));
    const again = await applyOnboardingPlan({ plan: second, root, github });
    assert.equal(again.overall_status, "repository_ready_external_pending");
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
    assert.equal(compliant.overall_status, "repository_ready_external_pending");
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
    assert.match(prompt, /enable native Auto-merge[\s\S]*APPROVE/);
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
