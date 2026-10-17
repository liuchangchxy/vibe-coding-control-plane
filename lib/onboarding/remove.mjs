import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { lstat, readFile } from "node:fs/promises";
import path from "node:path";
import { parseManifest } from "./manifest.mjs";
import { CLEANUP_ADAPTER_PATH, LOCK_PATH, MANIFEST_PATH, PR_TEMPLATE_PATH, REVIEWER_PROMPT_PATH } from "./constants.mjs";
import { checkoutMatchesRepo, inspectCheckout } from "./git-repository.mjs";
import { canonicalManagedText, hashManagedText, sha256 } from "./render.mjs";

const SOURCE = "liuchangchxy/vibe-coding-control-plane";
const OWNABLE = new Set([CLEANUP_ADAPTER_PATH, REVIEWER_PROMPT_PATH, PR_TEMPLATE_PATH]);
const RETAINED = ["GitHub labels", "branch protection and rulesets", "Issues and pull requests"];
const planHash = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

async function fileState(root, file) {
  const target = path.join(root, file);
  let cursor = root;
  for (const part of file.split(path.sep).slice(0, -1)) {
    cursor = path.join(cursor, part);
    try {
      const stat = await lstat(cursor);
      if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`unsafe path component: ${cursor}`);
    } catch (error) { if (error.code === "ENOENT") return null; throw error; }
  }
  try {
    const stat = await lstat(target);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`unsafe target path: ${target}`);
    return await readFile(target);
  } catch (error) { if (error.code === "ENOENT") return null; throw error; }
}

function blocked(plan, reason) {
  plan.overall_status = "repository_blocked";
  plan.errors.push(reason);
  plan.plan_id = planHash(plan);
  return plan;
}

export async function createRemovePlan({ repo, root, github, host = "github.com" }) {
  const plan = { plan_schema_version: 1, command: "remove", repo, items: [], errors: [],
    managed_repository_removals: [], machine_actions: ["stop Consumer runtime", "verify Runner and RuntimeLock released", "remove Consumer launcher", "unregister Consumer runtime binding"],
    external_retained: [...RETAINED, "shared credentials and provider applications", "SQLite runtime history", "shared exact-revision runtime checkouts"] };
  let checkout;
  try { checkout = inspectCheckout(root); }
  catch (error) { return blocked(plan, error.message); }
  if (!checkoutMatchesRepo(checkout, repo, host)) return blocked(plan, `checkout remote must identify ${host}/${repo}`);
  let repository;
  try { repository = await github.getRepository(repo); }
  catch (error) { return blocked(plan, `cannot read target repository: ${error.message}`); }
  if (repository?.full_name?.toLowerCase() !== repo.toLowerCase()) return blocked(plan, `GitHub repository identity did not match ${repo}`);

  let manifestBytes;
  let lockBytes;
  try { [manifestBytes, lockBytes] = await Promise.all([fileState(root, MANIFEST_PATH), fileState(root, LOCK_PATH)]); }
  catch (error) { return blocked(plan, `cannot safely inspect Consumer files: ${error.message}`); }
  let manifest = null;
  if (manifestBytes) {
    try { manifest = parseManifest(manifestBytes.toString("utf8")); }
    catch (error) { return blocked(plan, `Consumer manifest ownership cannot be established: ${error.message}`); }
    if (manifest.vccp?.source !== SOURCE) return blocked(plan, "Consumer manifest does not identify the canonical VCCP source");
  }
  let lock = null;
  if (lockBytes) {
    try { lock = JSON.parse(lockBytes.toString("utf8")); }
    catch { return blocked(plan, "installation lock is not valid JSON"); }
    if (lock?.lock_schema_version !== 1 || lock.vccp?.source !== SOURCE || !Array.isArray(lock.artifacts)) {
      return blocked(plan, "installation lock ownership/schema is invalid");
    }
    if (!manifest) return blocked(plan, "installation lock exists without its VCCP manifest; ownership is ambiguous");
    if (lock.vccp.revision !== manifest.vccp.revision) return blocked(plan, "installation lock revision does not match the Consumer manifest");
    const seen = new Set();
    for (const record of lock.artifacts) {
      const expectedComponent = record.path === CLEANUP_ADAPTER_PATH ? "coordination-label-cleanup-adapter"
        : record.path === REVIEWER_PROMPT_PATH ? "reviewer-task-prompt"
          : record.path === PR_TEMPLATE_PATH ? "optional-pull-request-template" : null;
      if (!OWNABLE.has(record.path) || record.component !== expectedComponent
          || record.component_revision !== manifest.vccp.revision
          || !/^[0-9a-f]{64}$/.test(record.sha256 ?? "") || seen.has(record.path)) {
        return blocked(plan, "installation lock contains unsupported or ambiguous artifact ownership");
      }
      seen.add(record.path);
      let bytes;
      try { bytes = await fileState(root, record.path); }
      catch (error) { return blocked(plan, `cannot safely inspect ${record.path}: ${error.message}`); }
      if (bytes && hashManagedText(canonicalManagedText(bytes.toString("utf8"))) !== record.sha256) {
        return blocked(plan, `managed artifact drifted and will not be removed: ${record.path}`);
      }
      if (bytes) plan.managed_repository_removals.push({ path: record.path, sha256: sha256(bytes), reason: "unchanged artifact ownership proven by installation lock" });
    }
  }
  if (manifestBytes) plan.managed_repository_removals.push({ path: MANIFEST_PATH, sha256: sha256(manifestBytes), reason: "valid VCCP Consumer contract" });
  if (lockBytes) plan.managed_repository_removals.push({ path: LOCK_PATH, sha256: sha256(lockBytes), reason: "valid VCCP installation provenance" });
  let baseBranch = manifest?.repository?.base_branch ?? repository.default_branch;
  if (!manifestBytes && !lockBytes) {
    try { baseBranch = execFileSync("git", ["-C", root, "branch", "--show-current"], { encoding: "utf8" }).trim(); }
    catch { return blocked(plan, "cannot determine current Consumer branch after removal"); }
  }
  if (!baseBranch) return blocked(plan, "cannot determine Consumer base branch for the maintenance PR");
  let branch;
  try { branch = await github.getBranch(repo, baseBranch); }
  catch (error) { return blocked(plan, `cannot read Consumer base branch: ${error.message}`); }
  if (!branch?.commit?.sha) return blocked(plan, `Consumer base branch ${baseBranch} is unavailable`);
  if (!manifestBytes && !lockBytes) {
    let localHead;
    let dirty;
    try {
      localHead = execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim().toLowerCase();
      dirty = execFileSync("git", ["-C", root, "status", "--porcelain=v1", "--untracked-files=all"], { encoding: "utf8" }).trim();
    } catch (error) { return blocked(plan, `cannot verify the removed Consumer checkout: ${error.message}`); }
    if (localHead !== branch.commit.sha.toLowerCase()) return blocked(plan, "removed Consumer checkout is not at the exact current remote branch revision");
    if (dirty) return blocked(plan, "removed Consumer checkout has local changes; clean it before machine removal");
    plan.repository_status = "removed";
    plan.overall_status = "repository_removed";
    plan.snapshot = { base_branch: baseBranch, base_branch_sha: branch.commit.sha, manifest_sha256: null, lock_sha256: null };
    plan.plan_id = planHash(plan);
    return plan;
  }
  plan.snapshot = { base_branch: baseBranch, base_branch_sha: branch.commit.sha, manifest_sha256: manifestBytes ? sha256(manifestBytes) : null, lock_sha256: lockBytes ? sha256(lockBytes) : null };
  plan.repository_status = manifestBytes || lockBytes ? "changes_planned" : "removed";
  plan.overall_status = manifestBytes || lockBytes ? "repository_changes_planned" : "repository_removed";
  plan.plan_id = planHash(plan);
  return plan;
}
