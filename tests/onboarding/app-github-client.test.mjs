import test from "node:test";
import assert from "node:assert/strict";
import { AppGitHubClient } from "../../lib/onboarding/app-github-client.mjs";

const CONSUMER = "liuchangchxy/dayspark";
const SOURCE = "liuchangchxy/vibe-coding-control-plane";

test("public VCCP revision reads do not widen the Consumer-scoped App credential", async () => {
  const requests = [];
  const client = new AppGitHubClient({
    runtimeConfig: { github_app: {}, target_repository: CONSUMER },
    publicFetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ name: "main", commit: { sha: "a".repeat(40) } }) };
    },
  });

  const branch = await client.getBranch(SOURCE, "main");
  const commit = await client.getCommit(SOURCE, "a".repeat(40));

  assert.equal(branch.commit.sha, "a".repeat(40));
  assert.equal(commit.commit.sha, "a".repeat(40));
  assert.equal(requests.length, 2);
  assert.ok(requests.every(({ url }) => url.startsWith("https://api.github.com/repos/liuchangchxy/vibe-coding-control-plane/")));
  assert.ok(requests.every(({ options }) => !Object.keys(options.headers).some((key) => key.toLowerCase() === "authorization")));
});
