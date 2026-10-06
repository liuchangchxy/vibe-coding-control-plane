import { spawnSync } from "node:child_process";

function endpointRepo(repo) {
  const [owner, name] = repo.split("/");
  if (!owner || !name || repo.split("/").length !== 2) throw new Error("repo must be OWNER/REPO");
  return `repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
}

function httpStatus(stderr) {
  return Number(stderr.match(/HTTP (\d{3})/i)?.[1] ?? 0);
}

export class GitHubApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "GitHubApiError";
    this.status = status;
  }
}

export class GhGitHubClient {
  constructor({ host = "github.com", ghPath = "gh" } = {}) {
    this.host = host;
    this.ghPath = ghPath;
  }

  async request(endpoint, { method = "GET", body, paginate = false, host = this.host } = {}) {
    const args = ["api", "--hostname", host];
    if (paginate) args.push("--paginate", "--slurp");
    if (method !== "GET") args.push("--method", method);
    if (body !== undefined) args.push("--input", "-");
    args.push(endpoint);
    const result = spawnSync(this.ghPath, args, {
      encoding: "utf8",
      input: body === undefined ? undefined : JSON.stringify(body),
      maxBuffer: 16 * 1024 * 1024,
      timeout: 30_000,
    });
    if (result.error) throw new GitHubApiError(`gh api could not run: ${result.error.message}`);
    if (result.status !== 0) throw new GitHubApiError(result.stderr.trim() || "gh api failed", httpStatus(result.stderr));
    if (!result.stdout.trim()) return null;
    try { return JSON.parse(result.stdout); }
    catch { throw new GitHubApiError(`gh api returned invalid JSON for ${endpoint}`); }
  }

  async getRepository(repo) {
    return this.request(endpointRepo(repo));
  }

  async getBranch(repo, branch) {
    try { return await this.request(`${endpointRepo(repo)}/branches/${encodeURIComponent(branch)}`); }
    catch (error) { if (error.status === 404) return null; throw error; }
  }

  async getCommit(source, sha) {
    try {
      // The VCCP source is canonical on github.com even when the target
      // consumer is hosted on a GitHub Enterprise instance.
      const commit = await this.request(`${endpointRepo(source)}/commits/${encodeURIComponent(sha)}`, { host: "github.com" });
      return commit?.sha?.toLowerCase() === sha.toLowerCase() ? commit : null;
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  }

  async listLabels(repo) {
    const pages = await this.request(`${endpointRepo(repo)}/labels?per_page=100`, { paginate: true });
    return (Array.isArray(pages) ? pages.flat() : []).map(({ name, color, description }) => ({ name, color, description }));
  }

  async createLabel(repo, label) {
    return this.request(`${endpointRepo(repo)}/labels`, { method: "POST", body: label });
  }

  async getBranchProtection(repo, branch) {
    try { return { readable: true, value: await this.request(`${endpointRepo(repo)}/branches/${encodeURIComponent(branch)}/protection`) }; }
    catch (error) {
      if (error.status === 404) return { readable: true, value: null };
      if (error.status === 403) return { readable: false, reason: "insufficient permission to read branch protection" };
      throw error;
    }
  }

  async listRulesets(repo) {
    try {
      return { readable: true, value: await this.request(`${endpointRepo(repo)}/rulesets?includes_parents=true&per_page=100`, { paginate: true }) };
    } catch (error) {
      if (error.status === 403) return { readable: false, reason: "insufficient permission to read effective repository rulesets" };
      throw error;
    }
  }
}
