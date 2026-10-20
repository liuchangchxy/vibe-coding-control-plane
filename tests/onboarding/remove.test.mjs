import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { createRemovePlan } from "../../lib/onboarding/remove.mjs";
import { publishMaintenancePlan, runOnboardingCli } from "../../lib/onboarding/cli.mjs";
import { createManifest, stringifyManifest } from "../../lib/onboarding/manifest.mjs";

const REPO = "example/consumer";
const REV = "a".repeat(40);
const sha = (text) => createHash("sha256").update(text).digest("hex");

function manifest() {
  return createManifest({ vccp: { source: "liuchangchxy/vibe-coding-control-plane", revision: REV },
    repository: { base_branch: "main", implementer_authors: ["builder[bot]"] },
    reviewer: { required_checks: [{ name: "CI", accepted_conclusions: ["success"] }] },
    merge: { method: "squash" }, reporting: { pull_request_template: true } });
}

test("remove plan deletes only lock-proven unchanged managed files and preserves labels/policies", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "vccp-remove-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    execFileSync("git", ["-C", root, "config", "user.email", "test@example.invalid"]);
    execFileSync("git", ["-C", root, "config", "user.name", "Test"]);
    const source = stringifyManifest(manifest());
    await mkdir(path.join(root, ".github/vccp"), { recursive: true });
    await mkdir(path.join(root, ".github/workflows"), { recursive: true });
    await writeFile(path.join(root, ".github/workflows/consumer-cleanup.yml"), "consumer-owned workflow\n");
    const prompt = "reviewer prompt\n";
    const workflow = "managed workflow\n";
    await writeFile(path.join(root, ".github/control-plane.yml"), source);
    await writeFile(path.join(root, ".github/vccp/reviewer-task-prompt.md"), prompt);
    await writeFile(path.join(root, ".github/workflows/vccp-coordination-label-cleanup.yml"), workflow);
    await writeFile(path.join(root, ".github/vccp/installation-lock.json"), JSON.stringify({ lock_schema_version: 1,
      vccp: { source: "liuchangchxy/vibe-coding-control-plane", revision: REV }, artifacts: [
        { path: ".github/vccp/reviewer-task-prompt.md", component: "reviewer-task-prompt", component_revision: REV, sha256: sha(prompt) },
        { path: ".github/workflows/vccp-coordination-label-cleanup.yml", component: "coordination-label-cleanup-adapter", component_revision: REV, sha256: sha(workflow) },
      ] }));
    const github = { getRepository: async () => ({ full_name: REPO }), getBranch: async () => ({ commit: { sha: "c".repeat(40) } }) };
    const plan = await createRemovePlan({ repo: REPO, root, github });
    assert.equal(plan.overall_status, "repository_changes_planned");
    assert.deepEqual(plan.managed_repository_removals.map(({ path: file }) => file).sort(), [
      ".github/control-plane.yml", ".github/vccp/installation-lock.json",
      ".github/vccp/reviewer-task-prompt.md", ".github/workflows/vccp-coordination-label-cleanup.yml",
    ]);
    assert.equal(plan.managed_repository_removals.some(({ path: file }) => file.endsWith("consumer-cleanup.yml")), false);
    assert.ok(plan.external_retained.includes("GitHub labels"));
    assert.ok(plan.external_retained.includes("SQLite runtime history"));
    assert.ok(plan.external_retained.includes("shared exact-revision runtime checkouts"));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("remove plan blocks when a lock-proven artifact has drifted", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "vccp-remove-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    await mkdir(path.join(root, ".github/vccp"), { recursive: true });
    await writeFile(path.join(root, ".github/control-plane.yml"), stringifyManifest(manifest()));
    await writeFile(path.join(root, ".github/vccp/reviewer-task-prompt.md"), "consumer edit\n");
    await writeFile(path.join(root, ".github/vccp/installation-lock.json"), JSON.stringify({ lock_schema_version: 1,
      vccp: { source: "liuchangchxy/vibe-coding-control-plane", revision: REV }, artifacts: [
        { path: ".github/vccp/reviewer-task-prompt.md", component: "reviewer-task-prompt", component_revision: REV, sha256: sha("original\n") },
      ] }));
    const github = { getRepository: async () => ({ full_name: REPO }), getBranch: async () => ({ commit: { sha: "c".repeat(40) } }) };
    const plan = await createRemovePlan({ repo: REPO, root, github });
    assert.equal(plan.overall_status, "repository_blocked");
    assert.equal(plan.managed_repository_removals.length, 0);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("remove opens one maintenance PR before machine cleanup, then unregisters idempotently", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "vccp-remove-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    execFileSync("git", ["-C", root, "config", "user.email", "test@example.invalid"]);
    execFileSync("git", ["-C", root, "config", "user.name", "Test"]);
    const source = stringifyManifest(manifest());
    await mkdir(path.join(root, ".github"), { recursive: true });
    await writeFile(path.join(root, ".gitignore"), "runtime.json\n");
    await writeFile(path.join(root, ".github/control-plane.yml"), source);
    const configPath = path.join(root, "runtime.json");
    const config = { database_path: path.join(root, "runtime.sqlite"), target_repository: REPO, workspace: root,
      enrollment: { authorized_repository: REPO }, implementer_provider: "builder", reviewer_provider: "reviewer",
      reviewer_github_identity: "reviewer-user", providers: [{ key: "reviewer", type: "claude" }], github_app: { credential_source: "/safe/key" } };
    await writeFile(configPath, JSON.stringify(config));
    execFileSync("git", ["-C", root, "add", "."]);
    execFileSync("git", ["-C", root, "commit", "-qm", "install VCCP"]);
    const github = { getRepository: async () => ({ full_name: REPO }), getBranch: async (_repo, name) => ({ name,
      commit: { sha: execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim() } }) };
    let removed = 0;
    let output = "";
    let code = await runOnboardingCli(["remove", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, maintenancePublisher: async ({ plan }) => ({ status: "open", number: 11, url: "https://github.com/example/consumer/pull/11", plan_id: plan.plan_id }),
      hostRemover: async () => { removed += 1; return { status: "removed" }; },
    });
    assert.equal(code, 2);
    assert.equal(JSON.parse(output).overall_status, "MAINTENANCE_PR_OPEN");
    assert.equal(removed, 0);
    await rm(path.join(root, ".github/control-plane.yml"));
    execFileSync("git", ["-C", root, "add", "-u"]);
    execFileSync("git", ["-C", root, "commit", "-qm", "merged VCCP removal"]);
    output = "";
    code = await runOnboardingCli(["remove", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, hostRemover: async () => { removed += 1; return { status: "removed" }; },
    });
    assert.equal(code, 0, output);
    assert.equal(JSON.parse(output).overall_status, "REMOVED");
    assert.equal(removed, 1);
    const updated = JSON.parse(await (await import("node:fs/promises")).readFile(configPath, "utf8"));
    assert.equal(updated.enrollment.authorized_repository, undefined);
    assert.equal(updated.reviewer_provider, undefined);
    assert.equal(updated.providers[0].key, "reviewer");
    assert.deepEqual(updated.github_app, { credential_source: "/safe/key" });
    output = "";
    code = await runOnboardingCli(["status", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, hostInspector: () => ({ status: "not_installed" }),
    });
    const status = JSON.parse(output);
    assert.equal(code, 2);
    assert.equal(status.overall_status, "NEEDS_ACTION");
    assert.equal(status.repository_status, "not_installed");
    assert.equal(status.machine_authorization.status, "required");
    assert.equal(status.machine_runtime.registration_status, "inactive_or_not_configured");
    assert.equal(status.host_runner.status, "not_installed");
    output = "";
    code = await runOnboardingCli(["remove", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github, hostRemover: async () => ({ status: "already_removed" }),
    });
    assert.equal(code, 0, output);
    assert.equal(JSON.parse(output).overall_status, "NOOP");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("remove blocks before PR or host mutation when machine config identity differs", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "vccp-remove-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    execFileSync("git", ["-C", root, "config", "user.email", "test@example.invalid"]);
    execFileSync("git", ["-C", root, "config", "user.name", "Test"]);
    await writeFile(path.join(root, ".gitignore"), "runtime.json\n");
    const configPath = path.join(root, "runtime.json");
    await writeFile(configPath, JSON.stringify({ target_repository: "other/repo", workspace: root }));
    execFileSync("git", ["-C", root, "add", "."]);
    execFileSync("git", ["-C", root, "commit", "-qm", "empty consumer"]);
    const github = { getRepository: async () => ({ full_name: REPO }), getBranch: async (_repo, name) => ({ name,
      commit: { sha: execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim() } }) };
    let output = "";
    const code = await runOnboardingCli(["remove", "--repo", REPO, "--path", root, "--runtime-config", configPath], {
      stdout: { write: (text) => { output += text; } }, stderr: { write: (text) => assert.fail(text) },
      githubFactory: () => github,
      maintenancePublisher: async () => assert.fail("wrong machine identity must block before PR publication"),
      hostRemover: async () => assert.fail("wrong machine identity must block before host mutation"),
    });
    assert.equal(code, 2);
    assert.equal(JSON.parse(output).overall_status, "BLOCKED");
    assert.match(JSON.parse(output).plan.errors[0], /target_repository/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("maintenance publisher creates a deterministic removal PR and adopts it on rerun", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "vccp-publish-remove-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "config", "user.email", "test@example.invalid"]);
    execFileSync("git", ["-C", root, "config", "user.name", "Test"]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    await mkdir(path.join(root, ".github"), { recursive: true });
    await writeFile(path.join(root, ".github/control-plane.yml"), "owned\n");
    execFileSync("git", ["-C", root, "add", "."]);
    execFileSync("git", ["-C", root, "commit", "-qm", "install"]);
    const base = execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    const calls = { tree: null, pulls: 0, refPermissions: [] };
    const prs = [];
    const github = {
      async listPullRequests() { return prs; },
      async getBranch() { return { commit: { sha: base } }; },
      async checkPermissions(_repo, permissions) { calls.permissionChecks ??= []; calls.permissionChecks.push(permissions); },
      async getCommit(_repo, shaValue) { return shaValue === base
        ? { sha: base, commit: { tree: { sha: "base-tree" } } }
        : { sha: shaValue, commit: { message: `VCCP remove ${REPO} plan ${plan.plan_id}` }, parents: [{ sha: base }] }; },
      async createBlob() { return { sha: "unused" }; },
      async createTree(_repo, tree) { calls.tree = tree; return { sha: "new-tree" }; },
      async createCommit() { return { sha: "d".repeat(40) }; },
      async createRef(_repo, ref, additionalPermissions) { calls.refPermissions.push(additionalPermissions); return { ref: ref.ref }; },
      async createPullRequest(_repo, pr) { calls.pulls += 1; const value = { number: 12, html_url: "https://github.com/example/consumer/pull/12", head: { ref: pr.head, sha: "d".repeat(40) }, base: { ref: "main" } }; prs.push(value); return value; },
    };
    const plan = { command: "remove", repo: REPO, plan_id: "f".repeat(64), snapshot: { base_branch: "main", base_branch_sha: base },
      items: [{ path: ".github/control-plane.yml", status: "delete", sha256: sha("owned\n") },
        { path: ".github/workflows/managed.yml", status: "delete" }] };
    const first = await publishMaintenancePlan({ plan, github, root });
    const second = await publishMaintenancePlan({ plan, github, root });
    assert.equal(first.status, "open", JSON.stringify(first));
    assert.equal(second.adopted, true);
    assert.equal(calls.pulls, 1);
    assert.deepEqual(calls.tree.tree, [
      { path: ".github/control-plane.yml", mode: "100644", type: "blob", sha: null },
      { path: ".github/workflows/managed.yml", mode: "100644", type: "blob", sha: null },
    ]);
    assert.deepEqual(calls.refPermissions[0], { workflows: "write" });
    assert.deepEqual(calls.permissionChecks, [{ workflows: "write" }]);
    assert.match(first.branch, /^codex-vccp-remove-/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("maintenance publisher checks workflow permission before any repository mutation", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "vccp-publish-workflow-"));
  try {
    execFileSync("git", ["init", "-q", root]);
    execFileSync("git", ["-C", root, "config", "user.email", "test@example.invalid"]);
    execFileSync("git", ["-C", root, "config", "user.name", "Test"]);
    execFileSync("git", ["-C", root, "remote", "add", "origin", `https://github.com/${REPO}.git`]);
    await writeFile(path.join(root, ".gitignore"), "runtime.json\n");
    execFileSync("git", ["-C", root, "add", "."]);
    execFileSync("git", ["-C", root, "commit", "-qm", "base"]);
    const base = execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    const calls = [];
    const github = {
      async listPullRequests() { return []; },
      async getBranch() { return { commit: { sha: base } }; },
      async checkPermissions(_repo, permissions) { calls.push(["checkPermissions", permissions]); throw new Error("permission denied"); },
      async createLabel() { calls.push(["createLabel"]); },
      async createBlob() { calls.push(["createBlob"]); },
      async createTree() { calls.push(["createTree"]); },
      async createCommit() { calls.push(["createCommit"]); },
      async createRef() { calls.push(["createRef"]); },
      async createPullRequest() { calls.push(["createPullRequest"]); },
    };
    const plan = { command: "deploy", repo: REPO, plan_id: "e".repeat(64), snapshot: { base_branch: "main", base_branch_sha: base },
      items: [{ path: ".github/workflows/managed.yml", status: "create", content: "name: managed\n" },
        { id: "labels", status: "create", create: [{ name: "VCCP / Ready" }] }] };
    const result = await publishMaintenancePlan({ plan, github, root });
    assert.equal(result.status, "unavailable");
    assert.match(result.detail, /permission denied/);
    assert.deepEqual(calls, [["checkPermissions", { workflows: "write" }]]);
  } finally { await rm(root, { recursive: true, force: true }); }
});
