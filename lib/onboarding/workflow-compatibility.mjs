import { readdir, readFile } from "node:fs/promises";
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
    .filter((reference) => reference.startsWith(".github/") && !reference.split("/").includes(".."));
}

function coordinationMutation(source) {
  const mentionsCoordinationLabels = COORDINATION_LABELS.some((label) => source.includes(label));
  const mutatesLabels = /removeLabel|remove-label|remove_labels/.test(source);
  return mentionsCoordinationLabels && mutatesLabels;
}

function behaviorConflict(source) {
  const terminalGuard = /TERMINAL_LABELS|infra-blocked|needs-human/.test(source)
    && /return\s*\{[^}]*removed\s*:\s*\[\]/s.test(source);
  if (terminalGuard) {
    return "existing terminal-label guard preserves active coordination labels; VCCP requires active labels to be removed after every Issue close";
  }
  return "existing automation mutates coordination labels on Issue close, but its behavior cannot be proven compatible with the VCCP cleanup invariant";
}

export async function findCleanupCompatibilityConflict(root, { managedWorkflowContent }) {
  const directory = path.join(root, WORKFLOW_DIRECTORY);
  let entries;
  try { entries = await readdir(directory, { withFileTypes: true }); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }

  for (const entry of entries) {
    if (!entry.isFile() || !/\.ya?ml$/i.test(entry.name)) continue;
    const relativePath = `${WORKFLOW_DIRECTORY}/${entry.name}`;
    const workflowPath = path.join(root, relativePath);
    const workflow = await readFile(workflowPath, "utf8");
    if (relativePath === ".github/workflows/vccp-coordination-label-cleanup.yml" && workflow === managedWorkflowContent) continue;
    if (!handlesIssueClosed(workflow)) continue;

    const referencedSources = [];
    for (const reference of localReferences(workflow)) {
      try { referencedSources.push(await readFile(path.join(root, reference), "utf8")); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    const behavior = [workflow, ...referencedSources].join("\n");
    if (coordinationMutation(behavior)) {
      return {
        path: relativePath,
        reason: behaviorConflict(behavior),
      };
    }
  }
  return null;
}
