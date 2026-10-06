import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { stringifyManifest } from "./manifest.mjs";
import { CLEANUP_ADAPTER_PATH, MANIFEST_PATH, PR_TEMPLATE_PATH, REVIEWER_PROMPT_PATH } from "./constants.mjs";

const TEMPLATE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../templates");
export const sha256 = (value) => createHash("sha256").update(value).digest("hex");

export async function renderArtifacts(manifest, { repo, templatesRoot = TEMPLATE_ROOT } = {}) {
  const adapterTemplate = await readFile(path.join(templatesRoot, "coordination-label-cleanup.yml"), "utf8");
  const reviewerTemplate = await readFile(path.join(templatesRoot, "reviewer-task-prompt.md"), "utf8");
  const output = new Map();
  output.set(MANIFEST_PATH, stringifyManifest(manifest));
  output.set(CLEANUP_ADAPTER_PATH, adapterTemplate.replaceAll("{{vccp.revision}}", manifest.vccp.revision));
  output.set(REVIEWER_PROMPT_PATH, reviewerTemplate
    .replaceAll("{{repository.full_name}}", repo)
    .replaceAll("{{repository.base_branch}}", manifest.repository.base_branch)
    .replaceAll("{{reviewer.required_checks}}", manifest.reviewer.required_checks.map(({ name, accepted_conclusions }) => `- ${JSON.stringify(name)}: ${accepted_conclusions.join(", ")}`).join("\n"))
    .replaceAll("{{vccp.revision}}", manifest.vccp.revision));
  if (manifest.reporting.pull_request_template) {
    output.set(PR_TEMPLATE_PATH, await readFile(path.join(templatesRoot, "pull_request_template.md"), "utf8"));
  }
  return output;
}

export function createLock(manifest, artifacts, existingLock = undefined) {
  const records = new Map((existingLock?.artifacts ?? []).map((record) => [record.path, record]));
  for (const [artifactPath, content] of artifacts) {
    if (artifactPath === MANIFEST_PATH) continue;
    records.set(artifactPath, {
      path: artifactPath,
      component: artifactPath === CLEANUP_ADAPTER_PATH ? "coordination-label-cleanup-adapter" :
        artifactPath === REVIEWER_PROMPT_PATH ? "reviewer-task-prompt" : "optional-pull-request-template",
      component_revision: manifest.vccp.revision,
      sha256: sha256(content),
    });
  }
  return {
    lock_schema_version: 1,
    vccp: { source: manifest.vccp.source, revision: manifest.vccp.revision },
    artifacts: [...records.values()].sort((left, right) => left.path.localeCompare(right.path)),
  };
}

export function stringifyLock(lock) {
  return `${JSON.stringify(lock, null, 2)}\n`;
}
