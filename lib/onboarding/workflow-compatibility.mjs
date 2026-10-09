import { realpath, readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { parseDocument } from "yaml";
import { ACTIVE_LABELS, TERMINAL_LABELS } from "./manifest.mjs";

const WORKFLOW_DIRECTORY = ".github/workflows";
const COORDINATION_LABELS = [...ACTIVE_LABELS, ...TERMINAL_LABELS];

function handlesIssueClosed(source) {
  const document = parseDocument(source, { uniqueKeys: true, strict: true });
  if (document.errors.length) return false;
  const events = document.toJS()?.on;
  if (!events || typeof events !== "object" || !events.issues) return false;
  const issues = events.issues;
  if (issues === true) return true;
  if (Array.isArray(issues)) return issues.includes("closed");
  if (typeof issues !== "object") return false;
  return !Array.isArray(issues.types) || issues.types.includes("closed");
}

function localReferences(source) {
  const workspaceReferences = [...source.matchAll(/GITHUB_WORKSPACE\}\s*\/(\.github\/[A-Za-z0-9._/-]+)/g)];
  const repositoryRelativeReferences = [...source.matchAll(/(?:^|[\s"'`])(\.github\/[A-Za-z0-9._/-]+)/gm)];
  return [...workspaceReferences, ...repositoryRelativeReferences]
    .map((match) => match[1])
    .filter((reference) => reference.startsWith(".github/")
      && !reference.split("/").includes("..")
      && !reference.split("/").includes("node_modules"));
}

function coordinationMutation(source) {
  const mentionsCoordinationLabels = COORDINATION_LABELS.some((label) => source.includes(label));
  const mutatesLabels = /removeLabel|remove-label|remove_labels/.test(source);
  return mentionsCoordinationLabels && mutatesLabels;
}

function cleanupBehavior(source) {
  const terminalEarlyReturn = /TERMINAL_LABELS|infra-blocked|needs-human/.test(source)
    && /return\s*\{[^}]*removed\s*:\s*\[\]/s.test(source);
  const removesActive = /ACTIVE_LABELS/.test(source)
    && ACTIVE_LABELS.every((label) => source.includes(label))
    && /removeLabel|remove-label|remove_labels/.test(source);
  const removesTerminal = /TERMINAL_LABELS/.test(source)
    && /removeLabel|remove-label|remove_labels/.test(source)
    && /for\s*\([^)]*TERMINAL_LABELS|removeLabel\([^)]*TERMINAL_LABELS/.test(source);
  const addsCoordination = /addLabel|add-label|add_labels/.test(source)
    && COORDINATION_LABELS.some((label) => source.includes(label));
  if (addsCoordination) return "Issue-close cleanup can re-add coordination labels; late cleanup must never reactivate work";
  if (terminalEarlyReturn) return "existing terminal-label guard preserves active coordination labels; VCCP requires active labels to be removed after every Issue close";
  if (!removesActive && removesTerminal) return "Issue-close cleanup removes terminal evidence instead of preserving it";
  if (!removesActive) return "existing issues.closed handler mutates coordination labels, but compatibility cannot be proven compatible because active-label cleanup semantics cannot be established";
  if (removesTerminal) return "Issue-close cleanup does not preserve terminal evidence";
  return null;
}

function isRelevantCleanupReference(reference) {
  const name = path.basename(reference, ".js");
  return reference.startsWith(".github/scripts/") && /cleanup/i.test(name) && /coordination|label/i.test(name);
}

async function readLocalReference(root, reference) {
  const repositoryRoot = await realpath(root);
  const literalPath = path.resolve(repositoryRoot, reference);
  const githubDirectory = path.join(repositoryRoot, ".github");
  if (!literalPath.startsWith(`${githubDirectory}${path.sep}`)) return { status: "unresolved" };
  async function readContained(candidate) {
    const actualPath = await realpath(candidate);
    const relative = path.relative(githubDirectory, actualPath);
    if (relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative)) return { status: "unresolved" };
    return { status: "read", source: await readFile(actualPath, "utf8") };
  }
  try { return await readContained(literalPath); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
  }

  if (!/^\.github\/scripts\/.+$/i.test(reference) || path.extname(reference)) return { status: "unresolved" };
  try { return await readContained(`${literalPath}.js`); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    return { status: "unresolved" };
  }
}

export async function inspectCleanupCapability(root, { managedWorkflowContent }) {
  const directory = path.join(root, WORKFLOW_DIRECTORY);
  let entries;
  try { entries = await readdir(directory, { withFileTypes: true }); }
  catch (error) { if (error.code === "ENOENT") return { status: "missing", provider: "vccp-managed" }; throw error; }

  let managedCleanup = false;
  let consumerCleanup = null;
  const conflicts = [];

  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    if (!entry.isFile() || !/\.ya?ml$/i.test(entry.name)) continue;
    const relativePath = `${WORKFLOW_DIRECTORY}/${entry.name}`;
    const workflowPath = path.join(root, relativePath);
    const workflow = await readFile(workflowPath, "utf8");
    if (relativePath === ".github/workflows/vccp-coordination-label-cleanup.yml" && workflow === managedWorkflowContent) {
      managedCleanup = true;
      continue;
    }
    if (!handlesIssueClosed(workflow)) continue;

    const references = localReferences(workflow);
    const referencedSources = [];
    let unresolvedCleanupReference = false;
    for (const reference of references) {
      const resolved = await readLocalReference(root, reference);
      if (resolved.status === "read") referencedSources.push(resolved.source);
      else if (isRelevantCleanupReference(reference)) unresolvedCleanupReference = true;
    }
    if (unresolvedCleanupReference) {
      conflicts.push({
        status: "conflict",
        provider: "consumer",
        path: relativePath,
        reason: "existing issues.closed coordination-label cleanup references a local behavior source that cannot be resolved; compatibility cannot be proven",
      });
      continue;
    }
    const behavior = [workflow, ...referencedSources].join("\n");
    if (!coordinationMutation(behavior)) continue;
    const reason = cleanupBehavior(behavior);
    if (reason) {
      conflicts.push({
        status: "conflict",
        provider: "consumer",
        path: relativePath,
        reason,
      });
      continue;
    }
    consumerCleanup ??= { status: "satisfied", provider: "consumer", path: relativePath,
      reason: "consumer cleanup removes active labels for every close, preserves terminal evidence, and only removes coordination state" };
  }
  if (conflicts.length) return conflicts[0];
  if (consumerCleanup) return consumerCleanup;
  if (managedCleanup) return { status: "satisfied", provider: "vccp-managed", path: ".github/workflows/vccp-coordination-label-cleanup.yml",
    reason: "VCCP-managed cleanup artifact is present and matches its generated content" };
  return { status: "missing", provider: "vccp-managed" };
}

export async function findCleanupCompatibilityConflict(root, options) {
  const capability = await inspectCleanupCapability(root, options);
  return capability.status === "conflict" ? capability : null;
}
