import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export class AppGitHubClient {
  constructor({ runtimeConfig, python = process.env.VCCP_PYTHON ?? (process.platform === "win32" ? "py" : "python3"),
    gitRunner = spawnSync, pythonRunner = spawnSync } = {}) {
    if (!runtimeConfig?.github_app) throw new Error("machine-local GitHub App config is required");
    this.runtimeConfig = runtimeConfig;
    this.python = python;
    this.gitRunner = gitRunner;
    this.pythonRunner = pythonRunner;
    this.sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  }

  publicGit(args) {
    let result;
    try {
      result = this.gitRunner("git", ["-c", "credential.helper=", ...args], {
        cwd: this.sourceRoot, encoding: "utf8", windowsHide: true, timeout: 30_000,
        env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : os.devNull,
          GIT_TERMINAL_PROMPT: "0" },
      });
    } catch {
      throw new Error("public GitHub source could not be inspected");
    }
    if (result.error || result.status === null) throw new Error("public GitHub source could not be inspected");
    return result;
  }

  async readPublicBranch(repo, branch) {
    if (!/^[^/]+\/[^/]+$/.test(repo) || !/^[A-Za-z0-9._/-]+$/.test(branch)
        || branch.includes("..") || branch.startsWith("-") || branch.endsWith("/")) {
      throw new Error("public GitHub branch reference is invalid");
    }
    const url = `https://github.com/${repo}.git`;
    const result = this.publicGit(["ls-remote", "--heads", url, `refs/heads/${branch}`]);
    if (result.status !== 0) throw new Error("public GitHub source branch could not be read");
    const row = (result.stdout ?? "").split(/\r?\n/).map((line) => line.split("\t"))
      .find(([sha, ref]) => ref === `refs/heads/${branch}` && /^[0-9a-f]{40}$/i.test(sha ?? ""));
    return row ? { name: branch, commit: { sha: row[0].toLowerCase() } } : null;
  }

  async readPublicCommit(repo, sha) {
    if (!/^[^/]+\/[^/]+$/.test(repo) || !/^[0-9a-f]{40}$/i.test(sha)) return null;
    const url = `https://github.com/${repo}.git`;
    const fetched = this.publicGit(["fetch", "--no-tags", url, sha.toLowerCase()]);
    if (fetched.status !== 0) return null;
    const result = this.publicGit(["cat-file", "-e", `${sha.toLowerCase()}^{commit}`]);
    return result.status === 0 ? { sha: sha.toLowerCase() } : null;
  }

  async request(action, repo, extra = {}) {
    const command = process.platform === "win32" && path.basename(this.python).toLowerCase() === "py"
      ? ["-3", "-m", "vccp_runtime.onboarding_github"]
      : ["-m", "vccp_runtime.onboarding_github"];
    const result = this.pythonRunner(this.python, command, {
      cwd: this.sourceRoot,
      input: JSON.stringify({ action, repo, runtime_config: this.runtimeConfig, ...extra }),
      encoding: "utf8", windowsHide: true, timeout: 60_000, maxBuffer: 4 * 1024 * 1024,
    });
    if (result.error) throw new Error(`GitHub App request could not run (${result.error.code ?? "spawn error"})`);
    let response;
    try { response = JSON.parse(result.stdout); }
    catch { throw new Error("GitHub App request returned an invalid response"); }
    if (!response.ok) {
      const error = new Error(`${response.error ?? "GitHub App request failed"}${response.action ? ` (${response.action})` : ""}`);
      error.status = response.status ?? 0;
      throw error;
    }
    return response.result;
  }

  getRepository(repo) { return this.request("getRepository", repo); }
  async getBranch(repo, branch) {
    try {
      return repo.toLowerCase() === this.runtimeConfig.target_repository?.toLowerCase()
        ? await this.request("getBranch", repo, { branch })
        : await this.readPublicBranch(repo, branch);
    }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  async getCommit(repo, sha) {
    try {
      return repo.toLowerCase() === this.runtimeConfig.target_repository?.toLowerCase()
        ? await this.request("getCommit", repo, { sha })
        : await this.readPublicCommit(repo, sha);
    }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  listLabels(repo) { return this.request("listLabels", repo); }
  createLabel(repo, label) { return this.request("createLabel", repo, { label }); }
  getBranchProtection(repo, branch) { return this.request("getBranchProtection", repo, { branch }); }
  listRulesets(repo) { return this.request("listRulesets", repo); }
  createBlob(repo, blob) { return this.request("createBlob", repo, { blob }); }
  createTree(repo, tree) { return this.request("createTree", repo, { tree }); }
  createCommit(repo, commit) { return this.request("createCommit", repo, { commit }); }
  checkPermissions(repo, permissions) { return this.request("checkPermissions", repo, { additional_permissions: permissions }); }
  createRef(repo, ref, additionalPermissions = {}) {
    return this.request("createRef", repo, { ref, additional_permissions: additionalPermissions });
  }
  async getRef(repo, branch) {
    try { return await this.request("getRef", repo, { branch }); }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  listPullRequests(repo) { return this.request("listPullRequests", repo); }
  createPullRequest(repo, pullRequest) { return this.request("createPullRequest", repo, { pull_request: pullRequest }); }
}
