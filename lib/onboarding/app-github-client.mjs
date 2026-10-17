import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

export class AppGitHubClient {
  constructor({ runtimeConfig, python = process.env.VCCP_PYTHON ?? "python3" } = {}) {
    if (!runtimeConfig?.github_app) throw new Error("machine-local GitHub App config is required");
    this.runtimeConfig = runtimeConfig;
    this.python = python;
    this.sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
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
    try { return await this.request("getBranch", repo, { branch }); }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  async getCommit(repo, sha) {
    try { return await this.request("getCommit", repo, { sha }); }
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
