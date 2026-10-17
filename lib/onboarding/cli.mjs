import { readFile, rename, writeFile } from "node:fs/promises";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { GhGitHubClient } from "./github-client.mjs";
import { AppGitHubClient } from "./app-github-client.mjs";
import { createOnboardingPlan } from "./plan.mjs";
import { applyOnboardingPlan, auditConsumer } from "./apply.mjs";
import { parseManifest } from "./manifest.mjs";

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

export async function runOnboardingCli(argv, { stdout = process.stdout, stderr = process.stderr, githubFactory = defaultGitHubFactory, runtimeRevision: injectedRuntimeRevision, runtimeQualifier = qualifyRuntime, runtimeLocator = locatePinnedRuntime, hostDeployer = deployPinnedRuntime, maintenancePublisher = publishMaintenancePlan, hostInspector = inspectHostRunner } = {}) {
  let command;
  let expectedRevision;
  try {
    [command, ...argv] = argv;
    if (!["plan", "apply", "audit", "readiness", "status", "deploy"].includes(command)) throw new Error("usage: vccp <plan|apply|audit|readiness|status|deploy> --repo OWNER/REPO --path CHECKOUT [--input FILE | --plan FILE | --runtime-config FILE | --propose-revision SHA]");
    const args = argumentsMap(argv);
    const allowed = new Set(["--repo", "--path", "--input", "--plan", "--runtime-config", "--propose-revision", "--host", "--implementer-provider", "--reviewer-provider", "--reviewer-identity"]);
    for (const key of Object.keys(args)) if (!allowed.has(key)) throw new Error(`unknown option ${key}`);
    const repo = args["--repo"];
    const root = args["--path"];
    const host = args["--host"] ?? "github.com";
    if (!repo || !root) throw new Error("--repo and --path are required");
    if (command === "deploy") {
      if (!args["--runtime-config"]) throw new Error("deploy requires --runtime-config");
      if (args["--plan"]) throw new Error("deploy derives a fresh PLAN and does not accept --plan");
      const input = args["--input"] ? await jsonFile(args["--input"], "--input") : undefined;
      const localConfig = await jsonFile(args["--runtime-config"], "--runtime-config");
      const github = githubFactory(host, localConfig);
      const canonical = await github.getBranch("liuchangchxy/vibe-coding-control-plane", "main");
      const canonicalRevision = canonical?.commit?.sha;
      if (typeof canonicalRevision !== "string" || !/^[0-9a-f]{40}$/i.test(canonicalRevision)) {
        throw new Error("cannot resolve the canonical VCCP main revision for deploy");
      }
      const manifestPath = path.join(root, ".github", "control-plane.yml");
      const installed = existsSync(manifestPath);
      const desiredRevision = args["--propose-revision"] ?? canonicalRevision;
      let currentRevision = null;
      if (installed) {
        try { currentRevision = parseManifest(await readFile(manifestPath, "utf8")).vccp.revision; }
        catch { /* PLAN will report the authoritative manifest error. */ }
      }
      if (!installed && input) input.vccp = { ...(input.vccp ?? {}), revision: desiredRevision };
      const plan = await createOnboardingPlan({ repo, root, host, github, input,
        revisionProposal: installed ? desiredRevision : undefined });
      let deployResult;
      if (plan.overall_status === "repository_blocked") {
        deployResult = { ...plan, command: "deploy", overall_status: "BLOCKED", exit_code: 2,
          next_action: { type: "resolve_repository_blocker", details: plan.errors ?? [] } };
      } else if (["repository_changes_planned", "repository_upgrade_required"].includes(plan.overall_status)) {
        let publication;
        try { publication = await maintenancePublisher({ plan, github, root, host }); }
        catch (error) { publication = { status: "unavailable", detail: error.message,
          http_status: error.status ?? 0 }; }
        deployResult = { command, repo, repository_status: plan.repository_status,
          desired_revision: plan.desired_manifest?.vccp?.revision ?? null,
          current_revision: currentRevision,
          plan_id: plan.plan_id,
          plan,
          changes: plan.items.filter(({ status }) => ["create", "update", "legacy"].includes(status))
            .map(({ id, path: itemPath, status }) => ({ id, ...(itemPath ? { path: itemPath } : {}), status })),
          external_steps: plan.external_steps,
          maintenance_pull_request: publication,
          machine_runtime_configured: Boolean(localConfig && typeof localConfig === "object"),
          overall_status: publication?.status === "open" ? "MAINTENANCE_PR_OPEN" : "CHANGES_REQUIRED",
          exit_code: 2,
          next_action: publication?.status === "open"
            ? { type: "maintenance_pr_review_and_merge", url: publication.url, number: publication.number,
              detail: "Review and merge the exact-head maintenance PR, then rerun deploy to resume from repository state." }
            : { type: "maintenance_pr_required", plan_id: plan.plan_id,
              detail: publication?.detail ?? "Consumer changes are ready; an authorized maintenance PR could not be opened." } };
      } else {
        const binding = bindMachineProviders(localConfig, args);
        if (binding.error) {
          deployResult = { command, repo, repository_status: plan.repository_status,
            desired_revision: plan.desired_manifest?.vccp?.revision ?? null,
            machine_runtime: binding.summary, overall_status: "MACHINE_NOT_READY", exit_code: 2,
            next_action: { type: "bind_local_agent_providers", detail: binding.error } };
        } else {
          if (binding.changed) await writeJsonAtomic(args["--runtime-config"], localConfig);
          const deployed = await hostDeployer({ repo, root, manifest: plan.desired_manifest,
            localConfig, manifestPath,
            runtimeConfigPath: path.resolve(args["--runtime-config"]), host });
          deployResult = { command, repo, repository_status: plan.repository_status,
            desired_revision: plan.desired_manifest?.vccp?.revision ?? null,
            runtime_deployment: deployed,
            machine_runtime: binding.summary,
            overall_status: deployed.status === "running" ? "RUNNING" : "MACHINE_NOT_READY",
            exit_code: deployed.status === "running" ? 0 : 2,
            ...(deployed.status === "running" ? {} : { next_action: { type: "resolve_runtime_blocker", detail: deployed.reason ?? "host deployment did not reach running state" } }) };
        }
      }
      stdout.write(`${JSON.stringify(deployResult, null, 2)}\n`);
      return deployResult.exit_code;
    }
    if (command === "readiness" || command === "status") {
      if (args["--input"] || args["--plan"] || args["--propose-revision"]) throw new Error("readiness accepts only --runtime-config");
      const localConfig = await jsonFile(args["--runtime-config"], "--runtime-config");
      const github = githubFactory(host, localConfig);
      const repositoryAudit = await auditConsumer({ repo, root, host, github });
      const manifest = repositoryAudit.desired_manifest;
      const hostRunner = hostInspector(repo, localConfig);
      let machineActivation;
      if (!manifest) {
        machineActivation = { status: "not_ready", blockers: ["repository contract manifest is unavailable"] };
      } else if (hostRunner.status === "running"
          && hostRunner.running_revision === manifest.vccp.revision.toLowerCase()
          && hostRunner.running_checkout_clean === true
          && hostRunner.config_matches === true) {
        machineActivation = { status: "ready", blockers: [], source: "verified_running_runtime" };
      } else {
        try {
          const pinnedRuntime = runtimeLocator(manifest.vccp.revision);
          if (pinnedRuntime) {
            if (pinnedRuntime.status !== "ready") throw new Error("the installed revision-specific runtime checkout is drifted");
            machineActivation = (await runtimeQualifier({ repo, manifest, localConfig, host,
              runtimeRoot: pinnedRuntime.path })).runtime_activation;
          } else {
            await assertRuntimeRevision(manifest.vccp.revision, injectedRuntimeRevision);
            machineActivation = (await runtimeQualifier({ repo, manifest, localConfig, host })).runtime_activation;
          }
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
      const deployedRevision = manifest?.vccp?.revision?.toLowerCase() ?? null;
      const hostAligned = hostRunner.status === "running"
        && hostRunner.running_revision === deployedRevision
        && hostRunner.running_checkout_clean === true;
      let deploymentState;
      if (repositoryStatus === "blocked") deploymentState = "BLOCKED";
      else if (!manifest) deploymentState = "NOT_INSTALLED";
      else if (repositoryStatus !== "ready") deploymentState = "CHANGES_REQUIRED";
      else if (hostRunner.status === "running" && !hostAligned) deploymentState = "RUNTIME_OUTDATED";
      else if (machineAuthorization.status !== "authorized" || machineActivation.status !== "ready") deploymentState = "MACHINE_NOT_READY";
      else if (hostAligned) deploymentState = "RUNNING";
      else deploymentState = "READY_TO_START";
      const result = {
        command,
        repo,
        repository_status: repositoryStatus,
        repository_audit: {
          overall_status: repositoryAudit.overall_status,
          capabilities: repositoryAudit.capabilities ?? {},
          errors: repositoryAudit.errors ?? [],
          items: (repositoryAudit.items ?? []).map(({ id, status, path, reason }) => ({ id, status, ...(path ? { path } : {}), ...(reason ? { reason } : {}) })),
        },
        installation_lock: {
          status: (repositoryAudit.items ?? []).find(({ id }) => id === "installation-lock")?.status ?? "unknown",
          path: ".github/vccp/installation-lock.json",
        },
        machine_authorization: machineAuthorization,
        machine_activation: machineActivation,
        runtime_activation: machineActivation,
        desired_revision: manifest?.vccp?.revision ?? null,
        runtime_checkout: manifest?.vccp?.revision ? runtimeLocator(manifest.vccp.revision) : null,
        machine_runtime: {
          implementer_provider: localConfig.implementer_provider ?? null,
          reviewer_provider: localConfig.reviewer_provider ?? null,
          reviewer_github_identity: localConfig.reviewer_github_identity ?? null,
          providers: (Array.isArray(localConfig.providers) ? localConfig.providers : []).map(({ key, type, executable }) => ({
            key: key ?? null,
            type: type ?? null,
            executable_configured: typeof executable === "string" && executable.length > 0,
          })),
        },
        host_runner: hostRunner,
        external: { reviewer_task: "unverified", branch_protection: repositoryAudit.branch_protection ?? "unverified",
          required_checks: manifest?.reviewer?.required_checks ?? [] },
        overall_status: command === "status" ? deploymentState
          : allReady ? "machine_activation_ready" : "machine_activation_blocked",
        exit_code: command === "status" ? (["RUNNING", "READY_TO_START"].includes(deploymentState) ? 0 : 2)
          : allReady ? 0 : 2,
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

function defaultGitHubFactory(host, localConfig) {
  return localConfig?.github_app ? new AppGitHubClient({ runtimeConfig: localConfig }) : new GhGitHubClient({ host });
}

function locatePinnedRuntime(revision) {
  if (typeof revision !== "string" || !/^[0-9a-f]{40}$/i.test(revision)) return null;
  const root = path.join(os.homedir(), ".vccp", "runtimes", revision.toLowerCase());
  if (!existsSync(root)) return null;
  try {
    if (lstatSync(root).isSymbolicLink()) return { status: "drift", revision, path: root };
    const actual = execFileSync("git", ["-C", root, "rev-parse", "--verify", "HEAD"], { encoding: "utf8" }).trim().toLowerCase();
    const dirty = execFileSync("git", ["-C", root, "status", "--porcelain=v1", "--untracked-files=all"], { encoding: "utf8" }).trim();
    if (actual !== revision.toLowerCase() || dirty) return { status: "drift", revision, path: root };
    return { status: "ready", revision: actual, path: root };
  } catch { return { status: "drift", revision, path: root }; }
}

function inspectHostRunner(repo, localConfig) {
  if (process.platform === "darwin") {
    const slug = repo.split("/").at(-1).toLowerCase().replace(/[^a-z0-9-]/g, "-");
    const label = localConfig.dispatcher?.label ?? `com.vccp.${slug}-dispatcher`;
    const plist = localConfig.dispatcher?.plist_path ?? path.join(os.homedir(), "Library", "LaunchAgents", `${label}.plist`);
    if (!existsSync(plist)) return { mechanism: "launchd", status: "not_installed", label };
    let uid = "";
    try { uid = execFileSync("id", ["-u"], { encoding: "utf8" }).trim(); }
    catch { return { mechanism: "launchd", status: "unknown", label, reason: "user id unavailable" }; }
    const loaded = spawnSync("launchctl", ["print", `gui/${uid}/${label}`], { encoding: "utf8", windowsHide: true });
    const source = readFileSync(plist, "utf8");
    const workingDirectory = source.match(/<key>WorkingDirectory<\/key>\s*<string>([^<]+)<\/string>/)?.[1] ?? null;
    let runningRevision = null;
    let runningCheckoutClean = null;
    if (workingDirectory) {
      try {
        runningRevision = execFileSync("git", ["-C", workingDirectory, "rev-parse", "--verify", "HEAD"], { encoding: "utf8" }).trim().toLowerCase();
        runningCheckoutClean = !execFileSync("git", ["-C", workingDirectory, "status", "--porcelain=v1", "--untracked-files=all"], { encoding: "utf8" }).trim();
      }
      catch { /* report an unknown revision below */ }
    }
    const pid = loaded.status === 0 ? Number(loaded.stdout.match(/\bpid\s*=\s*(\d+)/)?.[1] ?? 0) || null : null;
    const manifestPath = source.match(/--manifest<\/string>\s*<string>([^<]+)<\/string>/)?.[1] ?? null;
    const runtimeConfigPath = source.match(/--runtime-config<\/string>\s*<string>([^<]+)<\/string>/)?.[1] ?? null;
    const expectedManifestHash = source.match(/<key>VCCPManifestSHA256<\/key>\s*<string>([0-9a-f]{64})<\/string>/)?.[1] ?? null;
    const expectedConfigHash = source.match(/<key>VCCPRuntimeConfigSHA256<\/key>\s*<string>([0-9a-f]{64})<\/string>/)?.[1] ?? null;
    const hashMatches = (file, expected) => {
      if (!file || !expected) return false;
      try { return createHash("sha256").update(readFileSync(file)).digest("hex") === expected; }
      catch { return false; }
    };
    const manifestMatches = hashMatches(manifestPath, expectedManifestHash);
    const configMatches = hashMatches(runtimeConfigPath, expectedConfigHash);
    return { mechanism: "launchd", status: pid ? "running" : loaded.status === 0 ? "loaded" : "stopped", label,
      pid, working_directory: workingDirectory, running_revision: runningRevision,
      running_checkout_clean: runningCheckoutClean,
      manifest_sha256_matches: manifestMatches,
      runtime_config_sha256_matches: configMatches,
      config_matches: manifestMatches && configMatches };
  }
  if (process.platform === "win32") {
    const sidecarPath = localConfig.dispatcher?.sidecar_path ?? localConfig.sidecar_path;
    if (!sidecarPath || !existsSync(sidecarPath)) return { mechanism: "windows_sidecar", status: "not_inspected" };
    try {
      const sidecar = JSON.parse(readFileSync(sidecarPath, "utf8"));
      return { mechanism: "windows_sidecar", status: "configured", path: sidecarPath,
        running_revision: sidecar.runtime_revision ?? null };
    } catch { return { mechanism: "windows_sidecar", status: "drift", path: sidecarPath }; }
  }
  return { mechanism: "unsupported", status: "not_inspected" };
}

function bindMachineProviders(localConfig, args) {
  const providers = Array.isArray(localConfig.providers) ? localConfig.providers : [];
  const keys = new Set(providers.map(({ key }) => key).filter((key) => typeof key === "string"));
  const existing = ["implementer_provider", "reviewer_provider", "reviewer_github_identity"];
  for (const key of existing) {
    const option = `--${key.replaceAll("_", "-")}`;
    const supplied = args[option];
    if (localConfig[key] && supplied && localConfig[key] !== supplied) {
      return { error: `${key} is already bound to ${localConfig[key]}; deploy will not change an existing binding` };
    }
  }
  const implementer = localConfig.implementer_provider ?? args["--implementer-provider"] ?? providers[0]?.key;
  const reviewer = localConfig.reviewer_provider ?? args["--reviewer-provider"]
    ?? providers.find(({ key }) => key && key !== implementer)?.key ?? implementer;
  const identity = localConfig.reviewer_github_identity ?? args["--reviewer-identity"];
  if (!implementer || !reviewer || !keys.has(implementer) || !keys.has(reviewer)) {
    return { error: "choose configured --implementer-provider and --reviewer-provider values; existing bindings are preserved" };
  }
  if (typeof identity !== "string" || !/^[A-Za-z0-9-]{1,39}$/.test(identity)) {
    return { error: "a GitHub user --reviewer-identity is required for the independent Local Reviewer" };
  }
  const changed = !localConfig.implementer_provider || !localConfig.reviewer_provider || !localConfig.reviewer_github_identity;
  localConfig.implementer_provider = implementer;
  localConfig.reviewer_provider = reviewer;
  localConfig.reviewer_github_identity = identity;
  return { changed, summary: { implementer_provider: implementer, reviewer_provider: reviewer,
    reviewer_github_identity: identity } };
}

async function writeJsonAtomic(file, value) {
  const destination = path.resolve(file);
  const temporary = `${destination}.vccp-${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  try { await rename(temporary, destination); }
  catch (error) { try { await (await import("node:fs/promises")).unlink(temporary); } catch {} throw error; }
}

async function deployPinnedRuntime({ repo, root, manifest, localConfig, manifestPath, runtimeConfigPath, host }) {
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const python = process.env.VCCP_PYTHON ?? (process.platform === "win32" ? "py" : "python3");
  const args = process.platform === "win32" && path.basename(python).toLowerCase() === "py"
    ? ["-3", "-m", "vccp_runtime.deploy"] : ["-m", "vccp_runtime.deploy"];
  const result = spawnSync(python, args, { cwd: sourceRoot, input: JSON.stringify({ repo, manifest,
    local_config: localConfig, manifest_path: manifestPath, runtime_config_path: runtimeConfigPath,
    source_root: sourceRoot, host }), encoding: "utf8", windowsHide: true, timeout: 120_000 });
  if (result.error) return { status: "blocked", reason: `host deploy entrypoint unavailable (${result.error.code ?? "spawn error"})` };
  try { return JSON.parse(result.stdout); }
  catch { return { status: "blocked", reason: `host deploy entrypoint failed (${result.status ?? "unknown exit"})` }; }
}

async function publishMaintenancePlan({ plan, github, root }) {
  if (typeof github.createBlob !== "function" || typeof github.createTree !== "function"
      || typeof github.createCommit !== "function" || typeof github.createRef !== "function"
      || typeof github.createPullRequest !== "function") {
    return { status: "unavailable", detail: "configured GitHub client cannot publish a reviewable maintenance PR" };
  }
  const branch = `codex-vccp-deploy-${plan.plan_id.slice(0, 12)}`;
  const open = await github.listPullRequests(plan.repo);
  const existing = (Array.isArray(open) ? open : []).find((item) => item.head?.ref === branch
    && item.base?.ref === plan.desired_manifest.repository.base_branch);
  if (existing) return { status: "open", number: existing.number, url: existing.html_url,
    head_sha: existing.head?.sha ?? null, adopted: true };

  const baseBranch = await github.getBranch(plan.repo, plan.desired_manifest.repository.base_branch);
  if (baseBranch?.commit?.sha?.toLowerCase() !== plan.snapshot?.base_branch_sha?.toLowerCase()) {
    return { status: "unavailable", detail: "Consumer base branch changed after PLAN; rerun deploy for a fresh snapshot" };
  }
  const localHead = execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  if (localHead.toLowerCase() !== plan.snapshot.base_branch_sha.toLowerCase()) {
    return { status: "unavailable", detail: "Consumer checkout is not at the exact configured base branch revision" };
  }

  const labelChanges = plan.items.find(({ id, status }) => id === "labels" && status === "create");
  for (const label of labelChanges?.create ?? []) {
    try { await github.createLabel(plan.repo, label); }
    catch (error) {
      if (error.status !== 422) throw error;
      const current = await github.listLabels(plan.repo);
      if (!current.some(({ name }) => name === label.name)) throw error;
    }
  }

  const changes = plan.items.filter(({ path: file, status }) => file && ["create", "update"].includes(status));
  const blobs = [];
  for (const item of changes) {
    const blob = await github.createBlob(plan.repo, { content: item.content, encoding: "utf-8" });
    blobs.push({ path: item.path, mode: "100644", type: "blob", sha: blob.sha });
  }
  if (!blobs.length) return { status: "unavailable", detail: "PLAN contains no repository file changes to publish" };
  const base = plan.snapshot?.base_branch_sha;
  if (!base) return { status: "unavailable", detail: "PLAN has no exact base branch revision" };
  const baseCommit = await github.getCommit(plan.repo, base);
  const tree = await github.createTree(plan.repo, { base_tree: baseCommit.commit.tree.sha, tree: blobs });
  const message = `VCCP deploy ${plan.repo} plan ${plan.plan_id}`;
  const commit = await github.createCommit(plan.repo, { message, tree: tree.sha, parents: [base] });
  let ref;
  try { ref = await github.createRef(plan.repo, { ref: `refs/heads/${branch}`, sha: commit.sha }); }
  catch (error) {
    if (error.status !== 422) throw error;
    const currentRef = await github.getRef(plan.repo, branch);
    const currentCommit = currentRef?.object?.sha ? await github.getCommit(plan.repo, currentRef.object.sha) : null;
    if (!String(currentCommit?.commit?.message ?? "").includes(plan.plan_id)) {
      return { status: "unavailable", detail: "deterministic maintenance branch exists with different content" };
    }
  }
  const pull = await github.createPullRequest(plan.repo, { title: `Deploy VCCP ${plan.desired_manifest.vccp.revision.slice(0, 12)}`,
    head: branch, base: plan.desired_manifest.repository.base_branch,
    body: `## VCCP maintenance deployment\n\nConsumer: \`${plan.repo}\`\nDesired VCCP revision: \`${plan.desired_manifest.vccp.revision}\`\nPLAN: \`${plan.plan_id}\`\n\nGenerated by \`vccp deploy\` from a fresh PLAN. Merge through the repository's authorized maintenance review path, then rerun deploy to continue host reconciliation.\n` });
  return { status: "open", number: pull.number, url: pull.html_url, head_sha: commit.sha,
    branch, ref_created: Boolean(ref?.ref) };
}

async function qualifyRuntime({ repo, manifest, localConfig, host = "github.com", runtimeRoot }) {
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const python = process.env.VCCP_PYTHON ?? (process.platform === "win32" ? "py" : "python3");
  const args = process.platform === "win32" && path.basename(python).toLowerCase() === "py" ? ["-3", "-m", "vccp_runtime.activation"] : ["-m", "vccp_runtime.activation"];
  const result = spawnSync(python, args, {
    cwd: runtimeRoot ?? sourceRoot,
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
