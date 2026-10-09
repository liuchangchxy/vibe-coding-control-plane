import { readFile } from "node:fs/promises";
import { execFileSync, spawnSync } from "node:child_process";
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

export async function runOnboardingCli(argv, { stdout = process.stdout, stderr = process.stderr, githubFactory = (host) => new GhGitHubClient({ host }), runtimeRevision: injectedRuntimeRevision, runtimeQualifier = qualifyRuntime } = {}) {
  let command;
  let expectedRevision;
  try {
    [command, ...argv] = argv;
    if (!["plan", "apply", "audit", "readiness"].includes(command)) throw new Error("usage: onboard-consumer.mjs <plan|apply|audit|readiness> --repo OWNER/REPO --path CHECKOUT [--input FILE | --plan FILE | --runtime-config FILE | --propose-revision SHA]");
    const args = argumentsMap(argv);
    const allowed = new Set(["--repo", "--path", "--input", "--plan", "--runtime-config", "--propose-revision", "--host"]);
    for (const key of Object.keys(args)) if (!allowed.has(key)) throw new Error(`unknown option ${key}`);
    const repo = args["--repo"];
    const root = args["--path"];
    const host = args["--host"] ?? "github.com";
    if (!repo || !root) throw new Error("--repo and --path are required");
    if (command === "readiness") {
      if (args["--input"] || args["--plan"] || args["--propose-revision"]) throw new Error("readiness accepts only --runtime-config");
      const localConfig = await jsonFile(args["--runtime-config"], "--runtime-config");
      const github = githubFactory(host);
      const repositoryAudit = await auditConsumer({ repo, root, host, github });
      const manifest = repositoryAudit.desired_manifest;
      let machineActivation;
      if (!manifest) {
        machineActivation = { status: "not_ready", blockers: ["repository contract manifest is unavailable"] };
      } else {
        try {
          await assertRuntimeRevision(manifest.vccp.revision, injectedRuntimeRevision);
          machineActivation = (await runtimeQualifier({ repo, manifest, localConfig, host })).runtime_activation;
        } catch (error) {
          machineActivation = { status: "not_ready", blockers: [error.message] };
        }
      }
      const enrollment = localConfig.enrollment?.authorized_repository;
      const machineAuthorization = {
        status: typeof enrollment === "string" && enrollment.toLowerCase() === repo.toLowerCase()
          ? "authorized" : "required",
        repository: typeof enrollment === "string" ? enrollment : null,
      };
      const repositoryStatus = repositoryAudit.repository_status
        ?? (repositoryAudit.overall_status === "repository_upgrade_required" ? "upgrade_required" : "blocked");
      const allReady = repositoryStatus === "ready"
        && machineAuthorization.status === "authorized"
        && machineActivation.status === "ready";
      const result = {
        command: "readiness",
        repo,
        repository_status: repositoryStatus,
        repository_audit: {
          overall_status: repositoryAudit.overall_status,
          capabilities: repositoryAudit.capabilities ?? {},
          errors: repositoryAudit.errors ?? [],
          items: (repositoryAudit.items ?? []).map(({ id, status, path, reason }) => ({ id, status, ...(path ? { path } : {}), ...(reason ? { reason } : {}) })),
        },
        machine_authorization: machineAuthorization,
        machine_activation: machineActivation,
        runtime_activation: machineActivation,
        overall_status: allReady ? "machine_activation_ready" : "machine_activation_blocked",
        exit_code: allReady ? 0 : 2,
      };
      stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return result.exit_code;
    }
    const github = githubFactory(host);
    let result;
    if (command === "plan") {
      if (args["--runtime-config"]) throw new Error("--runtime-config is only valid for audit or readiness");
      if (args["--plan"]) throw new Error("--plan is only valid for apply");
      const input = args["--input"] ? await jsonFile(args["--input"], "--input") : undefined;
      expectedRevision = args["--propose-revision"] ?? input?.vccp?.revision;
      result = await createOnboardingPlan({ repo, root, host, github, input, revisionProposal: args["--propose-revision"] });
    } else if (command === "apply") {
      if (args["--runtime-config"]) throw new Error("--runtime-config is only valid for audit or readiness");
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
      if (args["--runtime-config"]) {
        const localConfig = await jsonFile(args["--runtime-config"], "--runtime-config");
        const qualification = await runtimeQualifier({ repo, manifest: result.desired_manifest, localConfig, host });
        result.runtime_activation = qualification.runtime_activation;
        const enrollment = localConfig.enrollment?.authorized_repository;
        result.machine_authorization = { status: typeof enrollment === "string" && enrollment.toLowerCase() === repo.toLowerCase()
          ? "authorized" : "required", repository: typeof enrollment === "string" ? enrollment : null };
        result.machine_activation = result.runtime_activation;
      }
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

async function qualifyRuntime({ repo, manifest, localConfig, host = "github.com" }) {
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const python = process.env.VCCP_PYTHON ?? (process.platform === "win32" ? "py" : "python3");
  const args = process.platform === "win32" && path.basename(python).toLowerCase() === "py" ? ["-3", "-m", "vccp_runtime.activation"] : ["-m", "vccp_runtime.activation"];
  const result = spawnSync(python, args, {
    cwd: sourceRoot,
    input: JSON.stringify({ repo, host, manifest, local_config: localConfig }),
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error) return { runtime_activation: { status: "not_ready", blockers: [`Python readiness entrypoint unavailable (${result.error.code ?? "spawn error"})`] } };
  try {
    const evidence = JSON.parse(result.stdout);
    return { runtime_activation: evidence };
  } catch {
    return { runtime_activation: { status: "not_ready", blockers: [`Python readiness entrypoint failed (${result.status ?? "unknown exit"})`] } };
  }
}
