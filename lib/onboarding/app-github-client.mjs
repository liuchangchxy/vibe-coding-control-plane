import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export class AppGitHubClient {
  constructor({ runtimeConfig, python = process.env.VCCP_PYTHON ?? "python3", gitRunner = spawnSync } = {}) {
    if (!runtimeConfig?.github_app) throw new Error("machine-local GitHub App config is required");
    this.runtimeConfig = runtimeConfig;
    this.python = python;
    this.gitRunner = gitRunner;
    this.sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  }

  publicGit(args) {
    let result;
    try {
      result = this.gitRunner("git", ["-c", "credential.helper=", ...args], {
        cwd: this.sourceRoot, encoding: "utf8", windowsHide: true, timeout: 30_000,
        env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: os.devNull,
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
    let result = this.publicGit(["cat-file", "-e", `${sha.toLowerCase()}^{commit}`]);
    if (result.status !== 0) {
      result = this.publicGit(["fetch", "--no-tags", url, sha.toLowerCase()]);
      if (result.status !== 0) return null;
      result = this.publicGit(["cat-file", "-e", `${sha.toLowerCase()}^{commit}`]);
    }
    return result.status === 0 ? { sha: sha.toLowerCase() } : null;
  }

  async request(action, repo, extra = {}) {
    const result = spawnSync(this.python, ["-m", "vccp_runtime.onboarding_github"], {
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
  createRef(repo, ref) { return this.request("createRef", repo, { ref }); }
  async getRef(repo, branch) {
    try { return await this.request("getRef", repo, { branch }); }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  listPullRequests(repo) { return this.request("listPullRequests", repo); }
  createPullRequest(repo, pullRequest) { return this.request("createPullRequest", repo, { pull_request: pullRequest }); }
}
