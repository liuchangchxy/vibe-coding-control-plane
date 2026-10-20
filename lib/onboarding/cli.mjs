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
import { createRemovePlan } from "./remove.mjs";
import { checkoutMatchesRepo, inspectCheckout } from "./git-repository.mjs";

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

export async function runOnboardingCli(argv, { stdout = process.stdout, stderr = process.stderr, githubFactory = defaultGitHubFactory, runtimeRevision: injectedRuntimeRevision, runtimeQualifier = qualifyRuntime, runtimeLocator = locatePinnedRuntime, hostDeployer = deployPinnedRuntime, hostRemover = removePinnedRuntime, maintenancePublisher = publishMaintenancePlan, hostInspector = inspectHostRunner, providerRebindChecker = inspectNoActiveExecutions } = {}) {
  let command;
  let expectedRevision;
  try {
    [command, ...argv] = argv;
    if (!["plan", "apply", "audit", "readiness", "status", "deploy", "remove"].includes(command)) throw new Error("usage: vccp <plan|apply|audit|readiness|status|deploy|remove> --repo OWNER/REPO --path CHECKOUT [--input FILE | --plan FILE | --runtime-config FILE | --propose-revision SHA]");
    const args = argumentsMap(argv);
    const allowed = new Set(["--repo", "--path", "--input", "--plan", "--runtime-config", "--propose-revision", "--host", "--implementer-provider", "--reviewer-provider", "--reviewer-identity", "--authorize-repository"]);
    for (const key of Object.keys(args)) if (!allowed.has(key)) throw new Error(`unknown option ${key}`);
    const repo = args["--repo"];
    const root = args["--path"];
    const host = args["--host"] ?? "github.com";
    if (!repo || !root) throw new Error("--repo and --path are required");
    if (command === "remove") {
      if (!args["--runtime-config"]) throw new Error("remove requires --runtime-config");
      if (args["--input"] || args["--plan"] || args["--propose-revision"]) throw new Error("remove derives a fresh ownership plan and accepts no desired-state input");
      const runtimeConfigPath = path.resolve(args["--runtime-config"]);
      const localConfig = await jsonFile(runtimeConfigPath, "--runtime-config");
      if (!localConfig || typeof localConfig !== "object" || Array.isArray(localConfig)) throw new Error("--runtime-config must contain a JSON object");
      const github = githubFactory(host, localConfig);
      const plan = await createRemovePlan({ repo, root, host, github });
      let machineIdentityError = null;
      if (typeof localConfig.target_repository !== "string" || localConfig.target_repository.toLowerCase() !== repo.toLowerCase()) {
        machineIdentityError = `runtime config target_repository must identify ${repo}`;
      } else if (typeof localConfig.workspace !== "string" || path.resolve(localConfig.workspace) !== path.resolve(root)) {
        machineIdentityError = "runtime config workspace must match the requested Consumer checkout path";
      } else {
        try {
          if (!checkoutMatchesRepo(inspectCheckout(localConfig.workspace), repo, host)) machineIdentityError = `runtime config workspace origin must identify ${host}/${repo}`;
        } catch (error) { machineIdentityError = `runtime config workspace cannot be verified: ${error.message}`; }
      }
      if (machineIdentityError) {
        plan.overall_status = "repository_blocked";
        plan.repository_status = "blocked";
        plan.errors.push(machineIdentityError);
        delete plan.plan_id;
        plan.plan_id = createHash("sha256").update(JSON.stringify(plan)).digest("hex");
      }
      if (plan.overall_status === "repository_blocked") {
        const result = { command, repo, overall_status: "BLOCKED", exit_code: 2, plan,
          next_action: { type: "resolve_remove_blocker", details: plan.errors } };
        stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return 2;
      }
      if (plan.overall_status === "repository_changes_planned") {
        plan.items = plan.managed_repository_removals.map((entry) => ({ id: `remove:${entry.path}`, path: entry.path, status: "delete", sha256: entry.sha256 }));
        let publication;
        try { publication = await maintenancePublisher({ plan, github, root, host }); }
        catch (error) { publication = { status: "unavailable", detail: error.message, http_status: error.status ?? 0 }; }
        const result = { command, repo, plan_id: plan.plan_id, plan,
          maintenance_pull_request: publication,
          overall_status: publication?.status === "open" ? "MAINTENANCE_PR_OPEN" : "CHANGES_REQUIRED",
          exit_code: 2,
          next_action: publication?.status === "open"
            ? { type: "maintenance_pr_review_and_merge", url: publication.url, number: publication.number, detail: "Merge the exact-head Remove PR, update the --path checkout to that merged base revision, then rerun vccp remove to continue machine cleanup." }
            : { type: "maintenance_pr_required", plan_id: plan.plan_id, detail: publication?.detail ?? "A reviewable Remove PR could not be opened." } };
        stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return 2;
      }
      const runtimeRemoval = await hostRemover({ repo, localConfig, runtimeConfigPath, host });
      if (runtimeRemoval?.status !== "removed" && runtimeRemoval?.status !== "already_removed") {
        const result = { command, repo, repository_status: "removed", runtime_removal: runtimeRemoval,
          overall_status: "BLOCKED", exit_code: 2,
          next_action: { type: "resolve_runtime_remove_blocker", detail: runtimeRemoval?.reason ?? "host runtime removal was not verified" } };
        stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        return 2;
      }
      const unregistered = unregisterConsumer(localConfig, repo);
      if (unregistered) await writeJsonAtomic(runtimeConfigPath, localConfig);
      const noOp = runtimeRemoval.status === "already_removed" && !unregistered;
      const result = { command, repo, repository_status: "removed", runtime_removal: runtimeRemoval,
        machine_registration: "inactive", preserved: plan.external_retained,
        overall_status: noOp ? "NOOP" : "REMOVED", exit_code: 0,
        next_action: { type: "none", detail: "Consumer is no longer managed by VCCP. Rerunning remove is safe." } };
      stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return 0;
    }
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
      let currentRevision = null;
      if (installed) {
        try { currentRevision = parseManifest(await readFile(manifestPath, "utf8")).vccp.revision; }
        catch { /* PLAN will report the authoritative manifest error. */ }
      }
      if (installed && localConfig.upgrade_target?.repository === repo.toLowerCase()) {
        const currentManifest = parseManifest(await readFile(manifestPath, "utf8"));
        fastForwardMergedConsumer(root, currentManifest.repository.base_branch);
        try { currentRevision = parseManifest(await readFile(manifestPath, "utf8")).vccp.revision; }
        catch { /* PLAN will report the authoritative manifest error. */ }
      }
      let lockedRevision = localConfig.upgrade_target?.repository === repo.toLowerCase()
        ? localConfig.upgrade_target.revision : null;
      if (lockedRevision && currentRevision?.toLowerCase() !== lockedRevision.toLowerCase()
          && currentRevision?.toLowerCase() !== localConfig.upgrade_target.from_revision?.toLowerCase()) {
        delete localConfig.upgrade_target;
        lockedRevision = null;
        await writeJsonAtomic(args["--runtime-config"], localConfig);
      }
      const desiredRevision = installed
        ? (lockedRevision ?? args["--propose-revision"] ?? currentRevision)
        : canonicalRevision;
      if (installed && args["--propose-revision"] && lockedRevision
          && args["--propose-revision"].toLowerCase() !== lockedRevision) {
        throw new Error(`an upgrade to ${lockedRevision} is already in progress`);
      }
      if (installed && args["--propose-revision"] && desiredRevision !== currentRevision) {
        localConfig.upgrade_target = { repository: repo.toLowerCase(), revision: desiredRevision.toLowerCase(),
          from_revision: currentRevision?.toLowerCase() ?? null };
      }
      if (!installed && input) input.vccp = { ...(input.vccp ?? {}), revision: desiredRevision };
      const plan = await createOnboardingPlan({ repo, root, host, github, input,
        revisionProposal: installed ? desiredRevision : undefined, upgradeLegacy: true });
      let deployResult;
      if (plan.overall_status === "repository_blocked") {
        if (localConfig.upgrade_target?.repository === repo.toLowerCase()) {
          delete localConfig.upgrade_target;
          await writeJsonAtomic(args["--runtime-config"], localConfig);
        }
        deployResult = { ...plan, command: "deploy", overall_status: "BLOCKED", exit_code: 2,
          next_action: { type: "resolve_repository_blocker", details: plan.errors ?? [] } };
      } else if (["repository_changes_planned", "repository_upgrade_required"].includes(plan.overall_status)) {
        let publication;
        try { publication = await maintenancePublisher({ plan, github, root, host }); }
        catch (error) { publication = { status: "unavailable", detail: error.message,
          http_status: error.status ?? 0 }; }
        if (publication?.status === "open" && args["--propose-revision"] && desiredRevision !== currentRevision) {
          localConfig.upgrade_target = { repository: repo.toLowerCase(), revision: desiredRevision.toLowerCase(),
            from_revision: currentRevision?.toLowerCase() ?? null };
          await writeJsonAtomic(args["--runtime-config"], localConfig);
        } else if (publication?.status !== "open" && localConfig.upgrade_target?.repository === repo.toLowerCase()) {
          delete localConfig.upgrade_target;
          await writeJsonAtomic(args["--runtime-config"], localConfig);
        }
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
        const binding = bindMachineProviders(localConfig, args, repo, providerRebindChecker);
        if (binding.error) {
          deployResult = { command, repo, repository_status: plan.repository_status,
            desired_revision: plan.desired_manifest?.vccp?.revision ?? null,
            machine_runtime: binding.summary, overall_status: "MACHINE_NOT_READY", exit_code: 2,
            next_action: { type: "bind_local_agent_providers", detail: binding.error } };
        } else {
          const enrollment = authorizeMachineEnrollment(localConfig, args, repo, root);
          if (enrollment.error) {
            deployResult = { command, repo, repository_status: plan.repository_status,
              desired_revision: plan.desired_manifest?.vccp?.revision ?? null,
              machine_runtime: binding.summary, overall_status: "MACHINE_NOT_READY", exit_code: 2,
              next_action: { type: "authorize_machine_enrollment", detail: enrollment.error } };
          } else {
            if (binding.changed || enrollment.changed) await writeJsonAtomic(args["--runtime-config"], localConfig);
            const deployed = await hostDeployer({ repo, root, manifest: plan.desired_manifest,
              localConfig, manifestPath,
              runtimeConfigPath: path.resolve(args["--runtime-config"]), host });
            if (deployed.status === "running" && localConfig.upgrade_target?.repository === repo.toLowerCase()
                && localConfig.upgrade_target.revision === plan.desired_manifest?.vccp?.revision?.toLowerCase()) {
              delete localConfig.upgrade_target;
              await writeJsonAtomic(args["--runtime-config"], localConfig);
            }
            deployResult = { command, repo, repository_status: plan.repository_status,
              desired_revision: plan.desired_manifest?.vccp?.revision ?? null,
              runtime_deployment: deployed,
              machine_runtime: { ...binding.summary, authorized_repository: repo },
              overall_status: deployed.status === "running" ? "RUNNING" : "MACHINE_NOT_READY",
              exit_code: deployed.status === "running" ? 0 : 2,
              ...(deployed.status === "running" ? {} : { next_action: { type: "resolve_runtime_blocker", detail: deployed.reason ?? "host deployment did not reach running state" } }) };
          }
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
      const hostRunner = hostInspector(repo, localConfig, {
        root, runtimeConfigPath: path.resolve(args["--runtime-config"]),
        manifestPath: path.join(root, ".github", "control-plane.yml"),
      });
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
      const manifestInstalled = existsSync(path.join(root, ".github", "control-plane.yml"));
      const repositoryStatus = !manifestInstalled ? "not_installed" : repositoryAudit.repository_status
        ?? (repositoryAudit.overall_status === "repository_upgrade_required" ? "upgrade_required" : "blocked");
      const allReady = repositoryStatus === "ready"
        && machineAuthorization.status === "authorized"
        && machineActivation.status === "ready";
      const deployedRevision = manifest?.vccp?.revision?.toLowerCase() ?? null;
      const hostAligned = hostRunner.status === "running"
        && hostRunner.running_revision === deployedRevision
        && hostRunner.running_checkout_clean === true
        && hostRunner.config_matches === true;
      let deploymentState;
      if ((hostRunner.status === "running" && (!manifest || !hostAligned))
          || (Boolean(manifest) && repositoryStatus === "blocked")
          || hostRunner.status === "blocked") deploymentState = "BLOCKED";
      else if (allReady && hostAligned) deploymentState = "READY";
      else deploymentState = "NEEDS_ACTION";
      const result = {
        command,
        repo,
        repository_status: repositoryStatus,
        repository_audit: {
          overall_status: !manifestInstalled ? "not_installed" : repositoryAudit.overall_status,
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
          registration_status: machineAuthorization.status === "authorized" ? "active" : "inactive_or_not_configured",
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
        exit_code: command === "status" ? (deploymentState === "READY" ? 0 : 2)
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

export function fastForwardMergedConsumer(root, baseBranch) {
  if (typeof baseBranch !== "string" || !baseBranch) return false;
  try {
    const branch = execFileSync("git", ["-C", root, "branch", "--show-current"], { encoding: "utf8" }).trim();
    if (branch !== baseBranch) return false;
    if (execFileSync("git", ["-C", root, "status", "--porcelain=v1", "--untracked-files=all"], { encoding: "utf8" }).trim()) return false;
    execFileSync("git", ["-C", root, "fetch", "--no-tags", "origin", baseBranch], { stdio: "ignore" });
    const target = execFileSync("git", ["-C", root, "rev-parse", "FETCH_HEAD"], { encoding: "utf8" }).trim();
    const head = execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    if (target.toLowerCase() === head.toLowerCase()) return false;
    execFileSync("git", ["-C", root, "merge-base", "--is-ancestor", "HEAD", target], { stdio: "ignore" });
    execFileSync("git", ["-C", root, "merge", "--ff-only", target], { stdio: "ignore" });
    return true;
  } catch { return false; }
}

function inspectHostRunner(repo, localConfig, context = {}) {
  if (process.platform === "darwin") {
    const slug = repo.split("/").map((part) => part.toLowerCase().replace(/[^a-z0-9-]/g, "-")).join("-");
    const suffix = createHash("sha256").update(repo.toLowerCase()).digest("hex").slice(0, 10);
    const label = localConfig.dispatcher?.label ?? `com.vccp.${slug.slice(0, 48)}-${suffix}-dispatcher`;
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
    const stableJson = (value) => Array.isArray(value) ? `[${value.map(stableJson).join(",")}]`
      : value && typeof value === "object" ? `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`
        : JSON.stringify(value);
    const hashMatches = (file, expected, runtimeConfig = false) => {
      if (!file || !expected) return false;
      try {
        if (runtimeConfig) {
          const config = JSON.parse(readFileSync(file, "utf8"));
          if (config && typeof config === "object" && !Array.isArray(config)) delete config.upgrade_target;
          return createHash("sha256").update(stableJson(config), "utf8").digest("hex") === expected;
        }
        return createHash("sha256").update(readFileSync(file)).digest("hex") === expected;
      }
      catch { return false; }
    };
    const manifestMatches = hashMatches(manifestPath, expectedManifestHash);
    const configMatches = hashMatches(runtimeConfigPath, expectedConfigHash, true);
    return { mechanism: "launchd", status: pid ? "running" : loaded.status === 0 ? "loaded" : "stopped", label,
      pid, working_directory: workingDirectory, running_revision: runningRevision,
      running_checkout_clean: runningCheckoutClean,
      manifest_sha256_matches: manifestMatches,
      runtime_config_sha256_matches: configMatches,
      config_matches: manifestMatches && configMatches };
  }
  if (process.platform === "win32") {
    const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
    const python = process.env.VCCP_PYTHON ?? "py";
    const args = path.basename(python).toLowerCase() === "py"
      ? ["-3", "-m", "vccp_runtime.deploy"] : ["-m", "vccp_runtime.deploy"];
    const inspected = spawnSync(python, args, { cwd: sourceRoot, encoding: "utf8", windowsHide: true,
      timeout: 30_000, input: JSON.stringify({ operation: "status", repo, local_config: localConfig,
        runtime_config_path: context.runtimeConfigPath, manifest_path: context.manifestPath }) });
    if (inspected.error) return { mechanism: "windows_task_scheduler", status: "blocked",
      reason: `Windows Runner status could not be inspected (${inspected.error.code ?? "spawn error"})` };
    try { return JSON.parse(inspected.stdout); }
    catch { return { mechanism: "windows_task_scheduler", status: "blocked",
      reason: `Windows Runner status inspection failed (${inspected.status ?? "unknown exit"})` }; }
  }
  return { mechanism: "unsupported", status: "not_inspected" };
}

function bindMachineProviders(localConfig, args, repo, providerRebindChecker) {
  const providers = Array.isArray(localConfig.providers) ? localConfig.providers : [];
  const keys = new Set(providers.map(({ key }) => key).filter((key) => typeof key === "string"));
  const existing = ["implementer_provider", "reviewer_provider", "reviewer_github_identity"];
  let rebindRequested = false;
  for (const key of existing) {
    const option = `--${key.replaceAll("_", "-")}`;
    const supplied = args[option];
    const reviewerSdkMigration = key === "reviewer_provider"
      && localConfig[key] === "claude_code" && supplied === "claude_agent_sdk";
    if (localConfig[key] && supplied && localConfig[key] !== supplied && !reviewerSdkMigration) rebindRequested = true;
  }
  const implementer = args["--implementer-provider"] ?? localConfig.implementer_provider;
  const reviewer = localConfig.reviewer_provider === "claude_code"
    && args["--reviewer-provider"] === "claude_agent_sdk"
    ? args["--reviewer-provider"]
    : (args["--reviewer-provider"] ?? localConfig.reviewer_provider);
  const identity = args["--reviewer-identity"] ?? localConfig.reviewer_github_identity;
  if (reviewer === "claude_agent_sdk") {
    if (!Array.isArray(localConfig.providers)) localConfig.providers = [];
    const sdkBindings = localConfig.providers.filter((provider) => provider?.key === reviewer);
    if (sdkBindings.length > 1 || sdkBindings.some((provider) => provider.type !== "claude_agent_sdk")) {
      return { error: "claude_agent_sdk provider configuration is duplicated or has the wrong type" };
    }
    if (sdkBindings.length === 0) {
      localConfig.providers.push({ key: reviewer, type: "claude_agent_sdk" });
      keys.add(reviewer);
    }
  }
  if (!implementer || !reviewer || !keys.has(implementer) || !keys.has(reviewer)) {
    return { error: "explicitly select configured --implementer-provider and --reviewer-provider values; existing bindings are preserved" };
  }
  if (typeof identity !== "string" || !/^[A-Za-z0-9-]{1,39}$/.test(identity)) {
    return { error: "a GitHub user --reviewer-identity is required for the independent Local Reviewer" };
  }
  if (rebindRequested) {
    const observed = providerRebindChecker(repo, localConfig);
    if (!observed || observed.status !== "ready") {
      return { error: observed?.reason ?? "active Consumer executions could not be inspected; provider binding was not changed" };
    }
  }
  const changed = localConfig.implementer_provider !== implementer || localConfig.reviewer_provider !== reviewer
    || localConfig.reviewer_github_identity !== identity;
  localConfig.implementer_provider = implementer;
  localConfig.reviewer_provider = reviewer;
  localConfig.reviewer_github_identity = identity;
  return { changed, summary: { implementer_provider: implementer, reviewer_provider: reviewer,
    reviewer_github_identity: identity } };
}

function inspectNoActiveExecutions(repo, localConfig) {
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const python = process.env.VCCP_PYTHON ?? (process.platform === "win32" ? "py" : "python3");
  const commandArgs = process.platform === "win32" && path.basename(python).toLowerCase() === "py"
    ? ["-3", "-m", "vccp_runtime.deploy"] : ["-m", "vccp_runtime.deploy"];
  const check = spawnSync(python, commandArgs, { cwd: sourceRoot, encoding: "utf8", windowsHide: true,
    timeout: 30_000, input: JSON.stringify({ operation: "provider-rebind-check", repo, local_config: localConfig }) });
  try { return JSON.parse(check.stdout); } catch { return null; }
}

function authorizeMachineEnrollment(localConfig, args, repo, root) {
  if (localConfig.enrollment !== undefined && localConfig.enrollment !== null
      && (typeof localConfig.enrollment !== "object" || Array.isArray(localConfig.enrollment))) {
    return { error: "runtime config enrollment must be an object before enrollment" };
  }
  const current = localConfig.enrollment?.authorized_repository;
  const requested = args["--authorize-repository"];
  if (typeof current === "string" && current.toLowerCase() !== repo.toLowerCase()) {
    return { error: `machine enrollment is already bound to ${current}; remove that binding before deploying ${repo}` };
  }
  if (requested && requested.toLowerCase() !== repo.toLowerCase()) {
    return { error: `--authorize-repository must match the requested Consumer ${repo}` };
  }
  if (typeof current === "string") return { changed: false };
  if (!requested) return { error: `human enrollment authorization is required; explicitly pass --authorize-repository ${repo}` };
  if (typeof localConfig.target_repository !== "string"
      || localConfig.target_repository.toLowerCase() !== repo.toLowerCase()) {
    return { error: `runtime config target_repository must identify ${repo} before enrollment` };
  }
  if (typeof localConfig.workspace !== "string"
      || path.resolve(localConfig.workspace) !== path.resolve(root)) {
    return { error: "runtime config workspace must match the requested Consumer checkout before enrollment" };
  }
  localConfig.enrollment = { ...(localConfig.enrollment ?? {}), authorized_repository: repo };
  return { changed: true };
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

async function removePinnedRuntime({ repo, localConfig, runtimeConfigPath, host }) {
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const python = process.env.VCCP_PYTHON ?? (process.platform === "win32" ? "py" : "python3");
  const args = process.platform === "win32" && path.basename(python).toLowerCase() === "py"
    ? ["-3", "-m", "vccp_runtime.deploy"] : ["-m", "vccp_runtime.deploy"];
  const result = spawnSync(python, args, { cwd: sourceRoot, input: JSON.stringify({ operation: "remove", repo,
    local_config: localConfig, runtime_config_path: runtimeConfigPath }),
    encoding: "utf8", windowsHide: true, timeout: 120_000 });
  if (result.error) return { status: "blocked", reason: `host remove entrypoint unavailable (${result.error.code ?? "spawn error"})` };
  try { return JSON.parse(result.stdout); }
  catch { return { status: "blocked", reason: `host remove entrypoint failed (${result.status ?? "unknown exit"})` }; }
}

function unregisterConsumer(localConfig, repo) {
  let changed = false;
  for (const key of ["implementer_provider", "reviewer_provider", "reviewer_github_identity"]) {
    if (Object.hasOwn(localConfig, key)) { delete localConfig[key]; changed = true; }
  }
  if (typeof localConfig.enrollment?.authorized_repository === "string"
      && localConfig.enrollment.authorized_repository.toLowerCase() === repo.toLowerCase()) {
    delete localConfig.enrollment.authorized_repository;
    changed = true;
  }
  return changed;
}

export async function publishMaintenancePlan({ plan, github, root }) {
  if (typeof github.createBlob !== "function" || typeof github.createTree !== "function"
      || typeof github.createCommit !== "function" || typeof github.createRef !== "function"
      || typeof github.createPullRequest !== "function") {
    return { status: "unavailable", detail: "configured GitHub client cannot publish a reviewable maintenance PR" };
  }
  const isRemove = plan.command === "remove";
  const baseBranchName = plan.desired_manifest?.repository?.base_branch ?? plan.snapshot?.base_branch;
  const branch = `codex-vccp-${isRemove ? "remove" : "deploy"}-${plan.plan_id.slice(0, 12)}`;
  const open = await github.listPullRequests(plan.repo);
  const existing = (Array.isArray(open) ? open : []).find((item) => item.head?.ref === branch
    && item.base?.ref === baseBranchName);
  if (existing) {
    const headSha = existing.head?.sha;
    const headCommit = headSha ? await github.getCommit(plan.repo, headSha) : null;
    const generatedMessage = headCommit?.commit?.message ?? "";
    const parentShas = (headCommit?.parents ?? []).map(({ sha }) => sha?.toLowerCase());
    if (headCommit?.sha?.toLowerCase() !== headSha?.toLowerCase()
        || !generatedMessage.includes(plan.plan_id)
        || !parentShas.includes(plan.snapshot?.base_branch_sha?.toLowerCase())) {
      return { status: "unavailable", detail: "deterministic maintenance PR head does not match this exact PLAN and base; resolve the branch before rerunning" };
    }
    return { status: "open", number: existing.number, url: existing.html_url, head_sha: headSha, adopted: true };
  }

  const baseBranch = await github.getBranch(plan.repo, baseBranchName);
  if (baseBranch?.commit?.sha?.toLowerCase() !== plan.snapshot?.base_branch_sha?.toLowerCase()) {
    return { status: "unavailable", detail: "Consumer base branch changed after PLAN; rerun deploy for a fresh snapshot" };
  }
  const localHead = execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  if (localHead.toLowerCase() !== plan.snapshot.base_branch_sha.toLowerCase()) {
    return { status: "unavailable", detail: "Consumer checkout is not at the exact configured base branch revision" };
  }
  const changes = plan.items.filter(({ path: file, status }) => file && ["create", "update", "delete"].includes(status));
  const workflowChanges = changes.some(({ path: file }) => file.startsWith(".github/workflows/"));
  if (workflowChanges && typeof github.checkPermissions !== "function") {
    return { status: "unavailable", detail: "GitHub client cannot preflight required workflow write permission" };
  }
  if (workflowChanges) {
    try { await github.checkPermissions(plan.repo, { workflows: "write" }); }
    catch (error) { return { status: "unavailable", detail: `GitHub App lacks workflow write permission: ${error.message}` }; }
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

  const blobs = [];
  for (const item of changes) {
    if (item.status === "delete") {
      if (item.sha256) {
        let localBytes;
        try { localBytes = await readFile(path.join(root, item.path)); }
        catch { return { status: "unavailable", detail: `${item.path} disappeared after Remove PLAN; rerun remove` }; }
        if (createHash("sha256").update(localBytes).digest("hex") !== item.sha256) {
          return { status: "unavailable", detail: `${item.path} changed after Remove PLAN; rerun remove` };
        }
      }
      blobs.push({ path: item.path, mode: "100644", type: "blob", sha: null });
      continue;
    }
    const blob = await github.createBlob(plan.repo, { content: item.content, encoding: "utf-8" });
    blobs.push({ path: item.path, mode: "100644", type: "blob", sha: blob.sha });
  }
  if (!blobs.length) return { status: "unavailable", detail: "PLAN contains no repository file changes to publish" };
  const base = plan.snapshot?.base_branch_sha;
  if (!base) return { status: "unavailable", detail: "PLAN has no exact base branch revision" };
  const baseCommit = await github.getCommit(plan.repo, base);
  const tree = await github.createTree(plan.repo, { base_tree: baseCommit.commit.tree.sha, tree: blobs });
  const message = `VCCP ${isRemove ? "remove" : "deploy"} ${plan.repo} plan ${plan.plan_id}`;
  const commit = await github.createCommit(plan.repo, { message, tree: tree.sha, parents: [base] });
  let ref;
  try { ref = await github.createRef(plan.repo, { ref: `refs/heads/${branch}`, sha: commit.sha },
    workflowChanges ? { workflows: "write" } : undefined); }
  catch (error) {
    if (error.status !== 422) throw error;
    const currentRef = await github.getRef(plan.repo, branch);
    const currentCommit = currentRef?.object?.sha ? await github.getCommit(plan.repo, currentRef.object.sha) : null;
    if (!String(currentCommit?.commit?.message ?? "").includes(plan.plan_id)) {
      return { status: "unavailable", detail: "deterministic maintenance branch exists with different content" };
    }
  }
  const pull = await github.createPullRequest(plan.repo, { title: isRemove ? `Remove VCCP management from ${plan.repo}` : `Deploy VCCP ${plan.desired_manifest.vccp.revision.slice(0, 12)}`,
    head: branch, base: baseBranchName,
    body: isRemove
      ? `## VCCP Consumer removal\n\nConsumer: \`${plan.repo}\`\nPLAN: \`${plan.plan_id}\`\n\nGenerated by \`vccp remove\` from a fresh ownership audit. Only VCCP-owned artifacts proven by the installation lock are removed. Merge through the repository's authorized maintenance review path, update the local Consumer checkout to the merged base revision, then rerun remove to stop and unregister the machine runtime.\n`
      : `## VCCP maintenance deployment\n\nConsumer: \`${plan.repo}\`\nDesired VCCP revision: \`${plan.desired_manifest.vccp.revision}\`\nPLAN: \`${plan.plan_id}\`\n\nGenerated by \`vccp deploy\` from a fresh PLAN. Merge through the repository's authorized maintenance review path, then rerun deploy to continue host reconciliation.\n` });
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
