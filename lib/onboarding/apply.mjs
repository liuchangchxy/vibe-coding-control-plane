import { createHash } from "node:crypto";
import { lstat, mkdir, readFile, rename, link, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { createOnboardingPlan } from "./plan.mjs";
import { CLEANUP_ADAPTER_PATH, LOCK_PATH, MANIFEST_PATH, PR_TEMPLATE_PATH, REVIEWER_PROMPT_PATH } from "./constants.mjs";

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const hashObject = (value) => sha256(JSON.stringify(value));
const isExpectedFile = (file) => file === MANIFEST_PATH || file === LOCK_PATH || [CLEANUP_ADAPTER_PATH, REVIEWER_PROMPT_PATH, PR_TEMPLATE_PATH].includes(file);

async function currentHash(root, relativePath) {
  try { return sha256(await readFile(path.join(root, relativePath))); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}

async function rejectSymlinkPath(root, relativePath) {
  let cursor = root;
  for (const part of relativePath.split(path.sep).slice(0, -1)) {
    cursor = path.join(cursor, part);
    try {
      const stat = await lstat(cursor);
      if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`unsafe non-directory path component: ${cursor}`);
    } catch (error) { if (error.code === "ENOENT") return; throw error; }
  }
  const target = path.join(root, relativePath);
  try { if ((await lstat(target)).isSymbolicLink()) throw new Error(`refusing to replace symlink: ${target}`); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
}

async function writeAtomic(root, relativePath, content, mode) {
  await rejectSymlinkPath(root, relativePath);
  const target = path.join(root, relativePath);
  await mkdir(path.dirname(target), { recursive: true });
  const temp = `${target}.vccp-${process.pid}-${cryptoRandom()}.tmp`;
  await writeFile(temp, content, { encoding: "utf8", mode: 0o644, flag: "wx" });
  try {
    if (mode === "create") {
      await link(temp, target); // link fails atomically with EEXIST; it never replaces a consumer file.
      await unlink(temp);
    } else {
      await rename(temp, target);
    }
  } catch (error) {
    try { await unlink(temp); } catch {}
    throw error;
  }
}

function cryptoRandom() {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function blockedResult(plan) {
  return { ...plan, command: "apply", overall_status: "repository_blocked", exit_code: 2, applied: [], errors: plan.errors ?? [] };
}

function staleResult(plan, reason, applied = []) {
  return {
    plan_schema_version: 1,
    command: "apply",
    repo: plan.repo,
    overall_status: "stale_plan",
    exit_code: 4,
    applied,
    errors: [reason],
  };
}

export async function applyOnboardingPlan({ plan, root, github, host = "github.com" }) {
  if (!plan || plan.plan_schema_version !== 1 || !plan.request || !plan.snapshot || !plan.plan_id) {
    return staleResult(plan ?? {}, "plan schema is missing or unsupported");
  }
  const { plan_id: claimedId, ...unsignedPlan } = plan;
  if (hashObject(unsignedPlan) !== claimedId) return staleResult(plan, "plan content hash does not match; generate a fresh PLAN");
  if (plan.items.some(({ status }) => status === "conflict" || status === "blocked")) return blockedResult(plan);

  let fresh;
  try {
    fresh = await createOnboardingPlan({
      repo: plan.request.repo,
      root,
      input: plan.request.input ?? undefined,
      revisionProposal: plan.request.revisionProposal ?? undefined,
      github,
      host,
    });
  } catch (error) { return staleResult(plan, `could not revalidate plan: ${error.message}`); }
  if (fresh.plan_id !== plan.plan_id) return staleResult(plan, "consumer files, GitHub state, or desired inputs changed after PLAN");

  const changes = plan.items.filter(({ status }) => status === "create" || status === "update");
  const labelItem = changes.find(({ id }) => id === "labels");
  const fileItems = changes.filter(({ path: file }) => file && file !== "labels");
  if (fileItems.some(({ path: file }) => !isExpectedFile(file))) return staleResult(plan, "plan contains an unsupported managed file path");

  const applied = [];
  try {
    const ordered = [...fileItems].sort((left, right) => {
      const rank = (item) => item.path === MANIFEST_PATH ? 3 : item.path === LOCK_PATH ? 2 : 1;
      return rank(left) - rank(right);
    });
    for (const entry of ordered) {
      const before = await currentHash(root, entry.path);
      const expected = entry.status === "create" ? null : entry.current;
      if (before !== expected) return staleResult(plan, `${entry.path} changed during APPLY preflight`);
    }
    for (const entry of ordered) {
      const before = await currentHash(root, entry.path);
      const expected = entry.status === "create" ? null : entry.current;
      if (before !== expected) return staleResult(plan, `${entry.path} changed immediately before APPLY`, applied);
      await writeAtomic(root, entry.path, entry.content, entry.status);
      applied.push({ id: entry.id, status: entry.status });
    }

    if (labelItem) {
      for (const label of labelItem.create) {
        try {
          await github.createLabel(plan.repo, label);
          applied.push({ id: `label:${label.name}`, status: "create" });
        } catch (error) {
          if (error.status === 422) {
            const current = await github.listLabels(plan.repo);
            if (current.some(({ name }) => name === label.name)) {
              applied.push({ id: `label:${label.name}`, status: "satisfied-race" });
              continue;
            }
          }
          throw error;
        }
      }
    }
  } catch (error) {
    return {
      plan_schema_version: 1,
      command: "apply",
      repo: plan.repo,
      overall_status: "command_failed",
      exit_code: 3,
      applied,
      errors: [error.message],
      external_steps: plan.external_steps,
    };
  }

  return {
    plan_schema_version: 1,
    command: "apply",
    repo: plan.repo,
    overall_status: "repository_ready_external_pending",
    exit_code: 0,
    applied,
    errors: [],
    external_steps: plan.external_steps,
    warnings: plan.warnings,
  };
}

export async function auditConsumer({ repo, root, github, host = "github.com" }) {
  const report = await createOnboardingPlan({ repo, root, github, host });
  report.command = "audit";
  if (report.overall_status === "repository_changes_planned") report.exit_code = 2;
  return report;
}
