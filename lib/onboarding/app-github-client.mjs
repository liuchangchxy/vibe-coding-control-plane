import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

export class AppGitHubClient {
  constructor({ runtimeConfig, python = process.env.VCCP_PYTHON ?? "python3", publicFetch = globalThis.fetch } = {}) {
    if (!runtimeConfig?.github_app) throw new Error("machine-local GitHub App config is required");
    this.runtimeConfig = runtimeConfig;
    this.python = python;
    this.publicFetch = publicFetch;
    this.sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  }

  async readPublic(repo, resource) {
    if (!/^[^/]+\/[^/]+$/.test(repo) || typeof this.publicFetch !== "function") {
      throw new Error("public GitHub read is unavailable");
    }
    const [owner, name] = repo.split("/");
    let response;
    try {
      response = await this.publicFetch(`https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/${resource}`, {
        headers: { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
      });
    } catch {
      throw new Error("public GitHub source could not be reached");
    }
    if (!response.ok) {
      const error = new Error(`public GitHub read failed (HTTP ${response.status})`);
      error.status = response.status;
      throw error;
    }
    return response.json();
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
        : await this.readPublic(repo, `branches/${encodeURIComponent(branch)}`);
    }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  async getCommit(repo, sha) {
    try {
      return repo.toLowerCase() === this.runtimeConfig.target_repository?.toLowerCase()
        ? await this.request("getCommit", repo, { sha })
        : await this.readPublic(repo, `commits/${encodeURIComponent(sha)}`);
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
