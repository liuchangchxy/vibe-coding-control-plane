import { ACTIVE_LABELS, CANONICAL_SOURCE, REVIEWER_EVENTS, TERMINAL_LABELS } from "./manifest.mjs";

export { ACTIVE_LABELS, CANONICAL_SOURCE, REVIEWER_EVENTS, TERMINAL_LABELS };
export const FROZEN_SPEC_LABEL = "frozen-spec";
export const LOCK_PATH = ".github/vccp/installation-lock.json";
export const MANIFEST_PATH = ".github/control-plane.yml";
export const REVIEWER_PROMPT_PATH = ".github/vccp/reviewer-task-prompt.md";
export const CLEANUP_ADAPTER_PATH = ".github/workflows/vccp-coordination-label-cleanup.yml";
export const PR_TEMPLATE_PATH = ".github/PULL_REQUEST_TEMPLATE/vccp.md";
export const ALLOWED_MANAGED_PATHS = Object.freeze([
  REVIEWER_PROMPT_PATH,
  CLEANUP_ADAPTER_PATH,
  PR_TEMPLATE_PATH,
]);

export const LABEL_DEFINITIONS = Object.freeze([
  { name: "agent-ready", color: "0e8a16", description: "Frozen specification is ready for an Implementer to claim." },
  { name: "agent-working", color: "1d76db", description: "Automation has claimed this Issue and work is in progress." },
  { name: "changes-requested", color: "d93f0b", description: "A formal REQUEST_CHANGES review requires same-PR repair." },
  { name: "infra-blocked", color: "5319e7", description: "Automation stopped because infrastructure is blocked." },
  { name: "needs-human", color: "b60205", description: "Automation stopped and requires a human decision." },
  { name: "frozen-spec", color: "fbca04", description: "This Issue contains an approved, frozen specification." },
]);
