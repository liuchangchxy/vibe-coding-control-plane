import { access, readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { parseDocument } from "yaml";
import { canonicalManagedText } from "./render.mjs";

const WORKFLOW_DIRECTORY = ".github/workflows";
const MANAGED_CLEANUP_PATH = `${WORKFLOW_DIRECTORY}/vccp-coordination-label-cleanup.yml`;

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

async function managedCleanupExists(root, managedWorkflowContent) {
  const workflowPath = path.join(root, MANAGED_CLEANUP_PATH);
  try {
    const existing = await readFile(workflowPath, "utf8");
    return canonicalManagedText(existing) === canonicalManagedText(managedWorkflowContent);
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

export async function inspectCleanupCapability(root, { managedWorkflowContent }) {
  const directory = path.join(root, WORKFLOW_DIRECTORY);
  let entries;
  try { entries = await readdir(directory, { withFileTypes: true }); }
  catch (error) { if (error.code === "ENOENT") entries = []; else throw error; }

  const managedCleanup = await managedCleanupExists(root, managedWorkflowContent);
  const consumerWorkflows = entries
    .filter((entry) => entry.isFile() && entry.name !== path.basename(MANAGED_CLEANUP_PATH)
      && /\.ya?ml$/i.test(entry.name))
    .sort((left, right) => left.name.localeCompare(right.name));

  for (const entry of consumerWorkflows) {
    const source = await readFile(path.join(directory, entry.name), "utf8");
    if (handlesIssueClosed(source)) {
      return { status: "satisfied", provider: "consumer", path: `${WORKFLOW_DIRECTORY}/${entry.name}`,
        reason: "consumer has an issues.closed workflow; VCCP manages its own label cleanup" };
    }
  }
  if (managedCleanup) return { status: "satisfied", provider: "vccp-managed", path: MANAGED_CLEANUP_PATH,
    reason: "VCCP-managed cleanup artifact is present and matches its generated content" };
  return { status: "missing", provider: "vccp-managed" };
}

export async function findCleanupCompatibilityConflict(root, options) {
  const target = path.join(root, MANAGED_CLEANUP_PATH);
  try {
    await access(target);
    const existing = await readFile(target, "utf8");
    if (canonicalManagedText(existing) !== canonicalManagedText(options.managedWorkflowContent)) {
      return { status: "conflict", provider: "consumer", path: MANAGED_CLEANUP_PATH,
        reason: "an existing workflow occupies the VCCP-managed cleanup path" };
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  return null;
}
