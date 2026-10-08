import { parseDocument, stringify } from "yaml";

export const CANONICAL_SOURCE = "liuchangchxy/vibe-coding-control-plane";
export const REVIEWER_EVENTS = Object.freeze([
  "pull_request.opened",
  "pull_request.ready_for_review",
  "pull_request.synchronize",
]);
export const ACTIVE_LABELS = Object.freeze(["agent-ready", "agent-working", "changes-requested"]);
export const TERMINAL_LABELS = Object.freeze(["infra-blocked", "needs-human"]);

export class ManifestError extends Error {
  constructor(errors) {
    super(errors.join("; "));
    this.name = "ManifestError";
    this.errors = errors;
  }
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function onlyKeys(value, keys, label, errors) {
  if (!isRecord(value)) {
    errors.push(`${label} must be an object`);
    return;
  }
  for (const key of Object.keys(value)) {
    if (!keys.includes(key)) errors.push(`${label}.${key} is not supported`);
  }
}

function nonPlaceholder(value, label, errors) {
  if (typeof value !== "string" || !value.trim() || /REPLACE_ME|<[^>]+>/.test(value)) {
    errors.push(`${label} must be a non-empty, completed value`);
  }
}

export function validateManifest(manifest) {
  const errors = [];
  onlyKeys(manifest, ["schema_version", "vccp", "repository", "issue_contract", "reviewer", "merge", "reporting"], "manifest", errors);
  if (!isRecord(manifest)) return errors;

  const legacy = manifest.schema_version === 1;
  if (!legacy && manifest.schema_version !== 2) errors.push("schema_version must be 2");
  onlyKeys(manifest.vccp, ["source", "revision"], "vccp", errors);
  if (isRecord(manifest.vccp)) {
    if (manifest.vccp.source !== CANONICAL_SOURCE) errors.push(`vccp.source must be ${CANONICAL_SOURCE}`);
    if (typeof manifest.vccp.revision !== "string" || !/^[0-9a-f]{40}$/i.test(manifest.vccp.revision)) {
      errors.push("vccp.revision must be a full 40-character commit SHA");
    }
  }

  onlyKeys(manifest.repository, ["base_branch", "implementer_authors"], "repository", errors);
  if (isRecord(manifest.repository)) {
    nonPlaceholder(manifest.repository.base_branch, "repository.base_branch", errors);
    const authors = manifest.repository.implementer_authors;
    if (!Array.isArray(authors) || !authors.length) errors.push("repository.implementer_authors must be a non-empty list");
    else {
      authors.forEach((name, index) => nonPlaceholder(name, `repository.implementer_authors[${index}]`, errors));
      if (new Set(authors).size !== authors.length) errors.push("repository.implementer_authors must not contain duplicates");
    }
  }

  onlyKeys(manifest.issue_contract, ["frozen_spec_label", "active_coordination_labels", "terminal_coordination_labels", legacy ? "max_request_changes_rounds" : "max_automated_repairs"], "issue_contract", errors);
  if (isRecord(manifest.issue_contract)) {
    if (manifest.issue_contract.frozen_spec_label !== "frozen-spec") errors.push("issue_contract.frozen_spec_label must be frozen-spec");
    if (JSON.stringify(manifest.issue_contract.active_coordination_labels) !== JSON.stringify(ACTIVE_LABELS)) errors.push("issue_contract.active_coordination_labels must match the VCCP invariant");
    if (JSON.stringify(manifest.issue_contract.terminal_coordination_labels) !== JSON.stringify(TERMINAL_LABELS)) errors.push("issue_contract.terminal_coordination_labels must match the VCCP invariant");
    const repairLimit = legacy ? manifest.issue_contract.max_request_changes_rounds : manifest.issue_contract.max_automated_repairs;
    if (repairLimit !== 3) errors.push(`issue_contract.${legacy ? "max_request_changes_rounds" : "max_automated_repairs"} must be 3`);
  }

  onlyKeys(manifest.reviewer, ["transport", "events", "exact_head_sha_required", "required_checks_completion", "required_checks"], "reviewer", errors);
  if (isRecord(manifest.reviewer)) {
    if (manifest.reviewer.transport !== "chatgpt-work-github-webhook-task") errors.push("reviewer.transport is a VCCP invariant");
    if (JSON.stringify(manifest.reviewer.events) !== JSON.stringify(REVIEWER_EVENTS)) errors.push("reviewer.events must be opened, ready_for_review, and synchronize");
    if (manifest.reviewer.exact_head_sha_required !== true) errors.push("reviewer.exact_head_sha_required must be true");
    if (manifest.reviewer.required_checks_completion !== "same_run_wait_until_terminal") errors.push("reviewer.required_checks_completion must use same-run waiting");
    const checks = manifest.reviewer.required_checks;
    if (!Array.isArray(checks) || !checks.length) errors.push("reviewer.required_checks must contain at least one consumer-defined check");
    else {
      const names = [];
      checks.forEach((check, index) => {
        onlyKeys(check, ["name", "accepted_conclusions"], `reviewer.required_checks[${index}]`, errors);
        if (!isRecord(check)) return;
        nonPlaceholder(check.name, `reviewer.required_checks[${index}].name`, errors);
        names.push(check.name);
        if (!Array.isArray(check.accepted_conclusions) || !check.accepted_conclusions.length) errors.push(`reviewer.required_checks[${index}].accepted_conclusions must be non-empty`);
        else check.accepted_conclusions.forEach((value, conclusionIndex) => nonPlaceholder(value, `reviewer.required_checks[${index}].accepted_conclusions[${conclusionIndex}]`, errors));
      });
      if (new Set(names).size !== names.length) errors.push("reviewer.required_checks names must be unique");
    }
  }

  onlyKeys(manifest.merge, ["native_auto_merge", "enable_native_auto_merge_by", "method", "direct_merge_forbidden", "admin_bypass_forbidden_in_normal_flow"], "merge", errors);
  if (isRecord(manifest.merge)) {
    if (manifest.merge.native_auto_merge !== true) errors.push("merge.native_auto_merge must be true");
    if (manifest.merge.enable_native_auto_merge_by !== "reviewer") errors.push("merge.enable_native_auto_merge_by must be reviewer");
    if (!["merge", "squash", "rebase"].includes(manifest.merge.method)) errors.push("merge.method must be merge, squash, or rebase");
    if (manifest.merge.direct_merge_forbidden !== true) errors.push("merge.direct_merge_forbidden must be true");
    if (manifest.merge.admin_bypass_forbidden_in_normal_flow !== true) errors.push("merge.admin_bypass_forbidden_in_normal_flow must be true");
  }

  onlyKeys(manifest.reporting, ["pull_request_template"], "reporting", errors);
  if (isRecord(manifest.reporting) && typeof manifest.reporting.pull_request_template !== "boolean") errors.push("reporting.pull_request_template must be boolean");

  return errors;
}

export function assertValidManifest(manifest) {
  const errors = validateManifest(manifest);
  if (errors.length) throw new ManifestError(errors);
  return manifest;
}

export function createManifest(input) {
  if (!isRecord(input)) throw new ManifestError(["onboarding input must be a JSON object"]);
  const allowed = ["vccp", "repository", "reviewer", "merge", "reporting"];
  for (const key of Object.keys(input)) if (!allowed.includes(key)) throw new ManifestError([`onboarding input.${key} is not supported`]);
  if (!isRecord(input.vccp) || input.vccp.source !== CANONICAL_SOURCE || !/^[0-9a-f]{40}$/i.test(input.vccp.revision ?? "")) {
    throw new ManifestError(["onboarding input.vccp must provide the canonical source and a full revision SHA"]);
  }
  if (!isRecord(input.repository) || !Array.isArray(input.repository.implementer_authors)) throw new ManifestError(["onboarding input.repository must include base_branch and implementer_authors"]);
  if (!isRecord(input.reviewer) || !Array.isArray(input.reviewer.required_checks) || Object.keys(input.reviewer).some((key) => key !== "required_checks")) throw new ManifestError(["onboarding input.reviewer may contain only consumer-defined required_checks"]);
  if (!isRecord(input.merge) || !["merge", "squash", "rebase"].includes(input.merge.method)) throw new ManifestError(["onboarding input.merge.method must be merge, squash, or rebase"]);
  if (input.reporting !== undefined && (!isRecord(input.reporting) || typeof input.reporting.pull_request_template !== "boolean")) throw new ManifestError(["onboarding input.reporting.pull_request_template must be boolean when provided"]);
  const manifest = {
    schema_version: 2,
    vccp: { source: CANONICAL_SOURCE, revision: input.vccp.revision.toLowerCase() },
    repository: input.repository,
    issue_contract: {
      frozen_spec_label: "frozen-spec",
      active_coordination_labels: [...ACTIVE_LABELS],
      terminal_coordination_labels: [...TERMINAL_LABELS],
      max_automated_repairs: 3,
    },
    reviewer: {
      transport: "chatgpt-work-github-webhook-task",
      events: [...REVIEWER_EVENTS],
      exact_head_sha_required: true,
      required_checks_completion: "same_run_wait_until_terminal",
      required_checks: input.reviewer.required_checks,
    },
    merge: {
      native_auto_merge: true,
      enable_native_auto_merge_by: "reviewer",
      method: input.merge.method,
      direct_merge_forbidden: true,
      admin_bypass_forbidden_in_normal_flow: true,
    },
    reporting: { pull_request_template: input.reporting?.pull_request_template === true },
  };
  assertValidManifest(manifest);
  return manifest;
}

export function parseManifest(source) {
  const document = parseDocument(source, { uniqueKeys: true, strict: true });
  if (document.errors.length) throw new ManifestError(document.errors.map((error) => error.message));
  const manifest = document.toJS();
  assertValidManifest(manifest);
  return manifest;
}

export function stringifyManifest(manifest) {
  assertValidManifest(manifest);
  return stringify(manifest, { lineWidth: 0 });
}

export function patchManifestRevision(source, revision) {
  if (!/^[0-9a-f]{40}$/i.test(revision)) throw new ManifestError(["revision proposal must be a full 40-character commit SHA"]);
  const document = parseDocument(source, { uniqueKeys: true, strict: true });
  if (document.errors.length) throw new ManifestError(document.errors.map((error) => error.message));
  const current = document.getIn(["vccp", "revision"]);
  if (typeof current !== "string") throw new ManifestError(["vccp.revision is missing from the consumer manifest"]);
  document.setIn(["vccp", "revision"], revision.toLowerCase());
  const output = document.toString({ lineWidth: 0 });
  parseManifest(output);
  return output;
}
