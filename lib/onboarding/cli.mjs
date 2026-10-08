import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { GhGitHubClient } from "./github-client.mjs";
import { createOnboardingPlan } from "./plan.mjs";
import { applyOnboardingPlan, auditConsumer } from "./apply.mjs";

function argumentsMap(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key.startsWith("--") || index + 1 >= argv.length || argv[index + 1].startsWith("--")) throw new Error(`expected --option value near ${key}`);
    if (result[key]) throw new Error(`duplicate option ${key}`);
    result[key] = argv[++index];
  }
  return result;
}

async function jsonFile(file, label) {
  if (!file) throw new Error(`${label} is required`);
  const source = await readFile(file, "utf8");
  try { return JSON.parse(source); }
  catch (error) { throw new Error(`${label} must be valid JSON: ${error.message}`); }
}

async function assertRuntimeRevision(expectedRevision, injectedRuntimeRevision) {
  if (typeof expectedRevision !== "string" || !/^[0-9a-f]{40}$/i.test(expectedRevision)) return;
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const runtimeRevision = (injectedRuntimeRevision ?? execFileSync("git", ["-C", sourceRoot, "rev-parse", "HEAD"], { encoding: "utf8" }).trim()).toLowerCase();
  if (!injectedRuntimeRevision) {
    const dirtySource = execFileSync("git", ["-C", sourceRoot, "status", "--porcelain"], { encoding: "utf8" }).trim();
    if (dirtySource) throw new Error("VCCP runtime checkout has uncommitted changes; use a clean checkout matching the desired full source SHA");
  }
  if (expectedRevision.toLowerCase() !== runtimeRevision) {
    throw new Error(`run this CLI from VCCP source revision ${expectedRevision}; current runtime is ${runtimeRevision}. Use an explicit revision proposal from the matching VCCP checkout to upgrade.`);
  }
}

export async function runOnboardingCli(argv, { stdout = process.stdout, stderr = process.stderr, githubFactory = (host) => new GhGitHubClient({ host }), runtimeRevision: injectedRuntimeRevision } = {}) {
  let command;
  let expectedRevision;
  try {
    [command, ...argv] = argv;
    if (!["plan", "apply", "audit"].includes(command)) throw new Error("usage: onboard-consumer.mjs <plan|apply|audit> --repo OWNER/REPO --path CHECKOUT [--input FILE | --plan FILE | --propose-revision SHA]");
    const args = argumentsMap(argv);
    const allowed = new Set(["--repo", "--path", "--input", "--plan", "--propose-revision", "--host"]);
    for (const key of Object.keys(args)) if (!allowed.has(key)) throw new Error(`unknown option ${key}`);
    const repo = args["--repo"];
    const root = args["--path"];
    const host = args["--host"] ?? "github.com";
    if (!repo || !root) throw new Error("--repo and --path are required");
    const github = githubFactory(host);
    let result;
    if (command === "plan") {
      if (args["--plan"]) throw new Error("--plan is only valid for apply");
      const input = args["--input"] ? await jsonFile(args["--input"], "--input") : undefined;
      expectedRevision = args["--propose-revision"] ?? input?.vccp?.revision;
      result = await createOnboardingPlan({ repo, root, host, github, input, revisionProposal: args["--propose-revision"] });
    } else if (command === "apply") {
      if (args["--input"] || args["--propose-revision"]) throw new Error("apply accepts a saved --plan only; create a fresh PLAN first");
      const plan = await jsonFile(args["--plan"], "--plan");
      if (plan.repo !== repo || plan.request?.repo !== repo) throw new Error("saved PLAN repository does not match --repo");
      expectedRevision = plan.desired_manifest?.vccp?.revision;
      if (plan.legacy_manifest?.status !== "upgrade_required") {
        await assertRuntimeRevision(expectedRevision, injectedRuntimeRevision);
      }
      result = await applyOnboardingPlan({ plan, root, host, github });
    } else {
      if (args["--input"] || args["--plan"] || args["--propose-revision"]) throw new Error("audit reads the committed manifest and accepts no desired-state input");
      result = await auditConsumer({ repo, root, host, github });
      expectedRevision = result.desired_manifest?.vccp?.revision;
    }
    if (command !== "apply" && result.overall_status !== "repository_blocked" && result.overall_status !== "repository_upgrade_required") {
      await assertRuntimeRevision(expectedRevision, injectedRuntimeRevision);
    }
    stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    return result.exit_code ?? 0;
  } catch (error) {
    stderr.write(`${JSON.stringify({ command: command ?? null, overall_status: "command_failed", exit_code: 3, errors: [error.message] }, null, 2)}\n`);
    return 3;
  }
}
