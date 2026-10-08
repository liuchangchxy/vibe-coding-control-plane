import { createHash } from "node:crypto";
import { lstat, readFile } from "node:fs/promises";
import path from "node:path";
import { assertValidManifest, createManifest, parseManifest, patchManifestRevision, stringifyManifest, ManifestError } from "./manifest.mjs";
import { ALLOWED_MANAGED_PATHS, CLEANUP_ADAPTER_PATH, LABEL_DEFINITIONS, LOCK_PATH, MANIFEST_PATH, PR_TEMPLATE_PATH, REVIEWER_PROMPT_PATH } from "./constants.mjs";
import { createLock, renderArtifacts, sha256, stringifyLock } from "./render.mjs";
import { checkoutMatchesRepo, inspectCheckout } from "./git-repository.mjs";

const jsonHash = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const isSha = (value) => typeof value === "string" && /^[0-9a-f]{40}$/i.test(value);

async function readState(root, relativePath) {
  const absolute = path.join(root, relativePath);
  let cursor = root;
  const parts = relativePath.split(path.sep);
  for (const part of parts.slice(0, -1)) {
    cursor = path.join(cursor, part);
    try {
      const stat = await lstat(cursor);
      if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`unsafe path component: ${cursor}`);
    } catch (error) { if (error.code === "ENOENT") return { exists: false, hash: null, content: null }; throw error; }
  }
  const absolutePath = path.join(cursor, parts.at(-1));
  let source;
  try {
    const stat = await lstat(absolutePath);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`unsafe target path: ${absolutePath}`);
    source = await readFile(absolutePath);
  }
  catch (error) { if (error.code === "ENOENT") return { exists: false, hash: null, content: null }; throw error; }
  return { exists: true, hash: sha256(source), content: source.toString("utf8") };
}

function parseLock(source) {
  if (source === null) return null;
  let lock;
  try { lock = JSON.parse(source); }
  catch { throw new Error("installation lock is not valid JSON"); }
  if (!lock || lock.lock_schema_version !== 1 || !lock.vccp || !Array.isArray(lock.artifacts)) throw new Error("installation lock schema is invalid");
  if (lock.vccp.source !== "liuchangchxy/vibe-coding-control-plane" || !isSha(lock.vccp.revision)) throw new Error("installation lock source/revision is invalid");
  const seen = new Set();
  for (const item of lock.artifacts) {
    if (!ALLOWED_MANAGED_PATHS.includes(item.path) || !isSha(item.component_revision) || !/^[0-9a-f]{64}$/.test(item.sha256) || seen.has(item.path)) {
      throw new Error("installation lock contains an invalid or duplicate artifact record");
    }
    seen.add(item.path);
  }
  return lock;
}

function classification(status) {
  if (status === "conflict" || status === "blocked") return "repository_blocked";
  return status;
}

function finishPlan(plan) {
  const statuses = plan.items.map(({ status }) => status);
  if (statuses.some((status) => status === "conflict" || status === "blocked")) plan.overall_status = "repository_blocked";
  else if (statuses.some((status) => status === "create" || status === "update")) plan.overall_status = "repository_changes_planned";
  else plan.overall_status = "repository_ready";
  plan.repository_status = plan.overall_status === "repository_blocked" ? "blocked"
    : plan.overall_status === "repository_upgrade_required" ? "upgrade_required"
      : plan.overall_status === "repository_changes_planned" ? "changes_planned" : "ready";
  plan.runtime_activation = { status: "unknown", reason: "machine-local runtime configuration was not supplied" };
  plan.exit_code = plan.overall_status === "repository_blocked" ? 2 : 0;
  plan.plan_id = jsonHash({ ...plan, plan_id: undefined });
  return plan;
}

function item(id, status, rest = {}) {
  return { id, status, ...rest };
}

function labelDigest(labels) {
  return jsonHash([...labels].map(({ name, color, description }) => ({ name, color, description: description ?? "" }))
    .sort((left, right) => left.name.localeCompare(right.name)));
}

function safeError(message, status = "blocked") {
  return { id: "preflight", status, current: null, desired: null, reason: message };
}

export async function createOnboardingPlan({ repo, root, input, revisionProposal, github, host = "github.com" }) {
  const plan = {
    plan_schema_version: 1,
    command: "plan",
    repo,
    items: [],
    external_steps: [
      { id: "reviewer-task", status: "external-step", observed: "not-queryable-by-local-cli", action: "Verify or create/update one ChatGPT Work GitHub webhook Reviewer Task for this repository and attach the rendered prompt." },
      { id: "branch-protection-rulesets", status: "external-step", observed: "read-only-audit", action: "Review current branch protection and effective rulesets; V1 does not write them." },
    ],
    warnings: [],
    errors: [],
    files: {},
    request: { repo, input: input ?? null, revisionProposal: revisionProposal ?? null },
  };

  let checkout;
  try { checkout = inspectCheckout(root); }
  catch (error) { plan.items.push(safeError(error.message)); return finishPlan(plan); }
  if (!checkoutMatchesRepo(checkout, repo, host)) {
    plan.items.push(safeError(`checkout remote must identify ${host}/${repo}`));
    return finishPlan(plan);
  }

  let repoInfo;
  try { repoInfo = await github.getRepository(repo); }
  catch (error) { plan.items.push(safeError(`cannot read target repository: ${error.message}`)); return finishPlan(plan); }
  if (repoInfo.full_name?.toLowerCase() !== repo.toLowerCase()) {
    plan.items.push(safeError(`GitHub repository identity did not match ${repo}`));
    return finishPlan(plan);
  }

  let existingManifestState;
  let existingLockState;
  try {
    [existingManifestState, existingLockState] = await Promise.all([
      readState(root, MANIFEST_PATH),
      readState(root, LOCK_PATH),
    ]);
  } catch (error) { plan.items.push(safeError(`cannot read consumer files: ${error.message}`)); return finishPlan(plan); }

  let manifest;
  let manifestContent;
  try {
    if (existingManifestState.exists) {
      if (input) throw new ManifestError(["fresh onboarding input is only accepted when the consumer manifest does not exist"]);
      manifest = parseManifest(existingManifestState.content);
      manifestContent = existingManifestState.content;
      if (revisionProposal !== undefined && revisionProposal !== null) {
        if (!isSha(revisionProposal)) throw new ManifestError(["revision proposal must be a full 40-character commit SHA"]);
        manifestContent = patchManifestRevision(manifestContent, revisionProposal.toLowerCase());
        manifest = parseManifest(manifestContent);
      }
    } else {
      if (!input) throw new ManifestError(["fresh repository requires explicit onboarding JSON input"]);
      if (revisionProposal) throw new ManifestError(["revision proposal requires an installed consumer manifest"]);
      manifest = createManifest(input);
      manifestContent = stringifyManifest(manifest);
    }
    assertValidManifest(manifest);
  } catch (error) {
    plan.errors.push(...(error.errors ?? [error.message]));
    plan.items.push(item("manifest", existingManifestState.exists ? "conflict" : "blocked", {
      path: MANIFEST_PATH,
      current: existingManifestState.exists ? existingManifestState.hash : "missing",
      desired: input ? "explicit onboarding input" : "valid consumer manifest or onboarding input",
      reason: error.message,
    }));
    return finishPlan(plan);
  }
  plan.manifest = manifest;

  if (manifest.schema_version === 1) {
    plan.desired_manifest = manifest;
    plan.legacy_manifest = { schema_version: 1, status: "upgrade_required", generic_runtime_supported: false };
    plan.items.push(item("manifest", "legacy", {
      path: MANIFEST_PATH,
      current: existingManifestState.hash,
      desired: "schema_version: 2",
      reason: "schema v1 is recognized for audit only and is unsupported by the generic runtime; an explicit v1-to-v2 change is required",
    }));
    plan.overall_status = "repository_upgrade_required";
    plan.repository_status = "upgrade_required";
    plan.exit_code = 0;
    plan.plan_id = jsonHash({ ...plan, plan_id: undefined });
    return plan;
  }

  const revision = manifest.vccp.revision;
  if (!isSha(revision)) {
    plan.items.push(safeError("VCCP revision must be a full commit SHA"));
    return finishPlan(plan);
  }
  try {
    const reachable = await github.getCommit(manifest.vccp.source, revision);
    if (!reachable || (typeof reachable === "object" && reachable.sha?.toLowerCase() !== revision.toLowerCase())) {
      plan.items.push(safeError(`VCCP revision ${revision} is not reachable from canonical GitHub source ${manifest.vccp.source}`));
      return finishPlan(plan);
    }
  } catch (error) {
    plan.items.push(safeError(`cannot verify canonical VCCP revision reachability: ${error.message}`));
    return finishPlan(plan);
  }

  const baseBranch = manifest.repository.base_branch;
  try {
    const branch = await github.getBranch(repo, baseBranch);
    if (!branch) {
      plan.items.push(safeError(`configured base branch ${baseBranch} does not exist in ${repo}`));
      return finishPlan(plan);
    }
    plan.repository = { identity: repoInfo.full_name, default_branch: repoInfo.default_branch, base_branch: baseBranch };
    plan.snapshot = { default_branch: repoInfo.default_branch, base_branch_sha: branch.commit?.sha ?? null };
  } catch (error) { plan.items.push(safeError(`cannot verify configured base branch: ${error.message}`)); return finishPlan(plan); }

  let lock;
  try { lock = parseLock(existingLockState.content); }
  catch (error) {
    plan.items.push(item("installation-lock", "conflict", { path: LOCK_PATH, current: existingLockState.hash, desired: "valid VCCP installation provenance", reason: error.message }));
    return finishPlan(plan);
  }
  if (lock && !existingManifestState.exists) {
    plan.items.push(item("installation-lock", "conflict", { path: LOCK_PATH, current: existingLockState.hash, desired: "manifest-backed installation lock", reason: "lock exists without a consumer-owned manifest" }));
    return finishPlan(plan);
  }
  if (lock && lock.vccp.revision !== parseManifest(existingManifestState.content).vccp.revision) {
    plan.items.push(item("installation-lock", "conflict", { path: LOCK_PATH, current: lock.vccp.revision, desired: parseManifest(existingManifestState.content).vccp.revision, reason: "lock provenance does not match the installed manifest revision" }));
    return finishPlan(plan);
  }

  let labels;
  try { labels = await github.listLabels(repo); }
  catch (error) { plan.items.push(safeError(`cannot read coordination labels: ${error.message}`)); return finishPlan(plan); }
  const labelByName = new Map(labels.map((label) => [label.name, label]));
  const missingLabels = [];
  for (const definition of LABEL_DEFINITIONS) {
    const current = labelByName.get(definition.name);
    if (!current) missingLabels.push(definition);
    else if ((current.color ?? "").toLowerCase() !== definition.color || (current.description ?? "") !== definition.description) {
      plan.warnings.push(`label metadata differs for ${definition.name}; identity exists, no mutation planned`);
    }
  }
  plan.items.push(item("labels", missingLabels.length ? "create" : "satisfied", {
    current: labels.map(({ name }) => name).sort((left, right) => left.localeCompare(right)),
    desired: LABEL_DEFINITIONS.map(({ name }) => name),
    create: missingLabels,
    reason: missingLabels.length ? "only missing standard label identities will be created" : "all standard label identities exist",
  }));

  let protection;
  let rulesets;
  try {
    const observations = await Promise.allSettled([
      github.getBranchProtection(repo, baseBranch),
      github.listRulesets(repo),
    ]);
    [protection, rulesets] = observations.map((result) => result.status === "fulfilled" ? result.value : ({ readable: false, reason: result.reason.message }));
  } catch (error) {
    protection = { readable: false, reason: error.message };
    rulesets = { readable: false, reason: error.message };
  }
  plan.protection_observation = {
    branch_protection: protection?.readable ? protection.value : { unavailable: protection?.reason ?? "not readable" },
    rulesets: rulesets?.readable ? rulesets.value : { unavailable: rulesets?.reason ?? "not readable" },
  };

  const artifacts = await renderArtifacts(manifest, { repo, sourceRevision: revision });
  artifacts.set(MANIFEST_PATH, manifestContent);
  const previousRecords = new Map((lock?.artifacts ?? []).map((record) => [record.path, record]));
  const artifactStatuses = [];
  const fileSnapshot = {};
  for (const [artifactPath, content] of artifacts) {
    const state = await readState(root, artifactPath);
    fileSnapshot[artifactPath] = state.hash;
    const previous = previousRecords.get(artifactPath);
    let status;
    let reason;
    if (!state.exists) {
      if (previous) { status = "conflict"; reason = "previously managed artifact is missing; VCCP will not recreate a removed file"; }
      else { status = "create"; reason = "path is absent"; }
    } else if (artifactPath === MANIFEST_PATH && revisionProposal && existingManifestState.exists && state.hash === existingManifestState.hash) {
      status = state.content === content ? "satisfied" : "update";
      reason = status === "update" ? "explicit VCCP revision proposal; only vccp.revision is patched" : "manifest already records the proposed revision";
    } else if (previous && state.hash !== previous.sha256) {
      status = "conflict";
      reason = "managed artifact drifted from its last generated hash";
    } else if (state.content === content) {
      status = "satisfied";
      reason = previous ? "managed artifact matches its lock hash and desired content" : "existing bytes already match desired output; eligible for provenance adoption";
    } else if (previous) {
      status = "update";
      reason = "managed artifact is unchanged from its lock hash and has a newer explicit desired revision";
    } else {
      status = "conflict";
      reason = "unmanaged consumer file occupies the desired path";
    }
    artifactStatuses.push(item(artifactPath === MANIFEST_PATH ? "manifest" : `file:${artifactPath}`, status, {
      path: artifactPath,
      current: state.hash ?? "missing",
      desired: sha256(content),
      reason,
      content,
    }));
  }

  for (const previous of lock?.artifacts ?? []) {
    if (!artifacts.has(previous.path)) {
      const state = await readState(root, previous.path);
      fileSnapshot[previous.path] = state.hash;
      if (state.exists && state.hash !== previous.sha256) {
        artifactStatuses.push(item(`file:${previous.path}`, "conflict", { path: previous.path, current: state.hash, desired: "reporting aid disabled", reason: "previously managed file has drifted; it will not be deleted" }));
      } else if (state.exists) {
        artifactStatuses.push(item(`file:${previous.path}`, "external-step", { path: previous.path, current: state.hash, desired: "reporting aid disabled", reason: "VCCP will not delete a previously managed file; remove it manually if desired" }));
      }
    }
  }

  const contentByPath = new Map(artifactStatuses.filter((entry) => entry.path && entry.content).map((entry) => [entry.path, entry.content]));
  if (artifactStatuses.some((entry) => entry.status === "conflict")) {
    plan.items.push(...artifactStatuses);
    plan.items.push(item("installation-lock", "blocked", { path: LOCK_PATH, current: existingLockState.hash ?? "missing", desired: "lock matching safe generated artifacts", reason: "lock cannot advance while any managed path conflicts" }));
    return finishPlan(plan);
  }

  const desiredLock = createLock(manifest, artifacts, lock ?? undefined);
  const lockContent = stringifyLock(desiredLock);
  const lockStatus = !existingLockState.exists ? "create" : existingLockState.content === lockContent ? "satisfied" : "update";
  plan.items.push(...artifactStatuses);
  plan.items.push(item("installation-lock", lockStatus, {
    path: LOCK_PATH,
    current: existingLockState.hash ?? "missing",
    desired: sha256(lockContent),
    reason: lockStatus === "satisfied" ? "lock matches exact generated bytes" : "lock records VCCP source revision and managed artifact hashes",
    content: lockContent,
  }));

  const manifestState = artifactStatuses.find((entry) => entry.id === "manifest");
  if (manifestState?.status === "conflict") {
    plan.items.push(item("manifest-integrity", "conflict", { path: MANIFEST_PATH, current: manifestState.current, desired: manifestState.desired, reason: manifestState.reason }));
  }

  plan.snapshot.labels_sha256 = labelDigest(labels);
  plan.snapshot.files = fileSnapshot;
  plan.snapshot.manifest_sha256 = existingManifestState.hash;
  plan.snapshot.lock_sha256 = existingLockState.hash;
  plan.snapshot.revision = revision;
  plan.desired_manifest = manifest;
  plan.generated_files = Object.fromEntries([...contentByPath, [LOCK_PATH, lockContent]]);
  plan.request = { repo, input: input ?? null, revisionProposal: revisionProposal ?? null };
  plan.items.push(item("protection-audit", "external-step", {
    current: plan.protection_observation,
    desired: { required_checks: manifest.reviewer.required_checks.map(({ name }) => name), pull_request_approval: true, native_auto_merge: true, no_normal_flow_bypass: true },
    reason: "read-only observation only; effective protection must be reviewed by a maintainer",
  }));
  return finishPlan(plan);
}

export { classification };
