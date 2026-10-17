import test from "node:test";
import assert from "node:assert/strict";
import { AppGitHubClient } from "../../lib/onboarding/app-github-client.mjs";

const CONSUMER = "liuchangchxy/dayspark";
const SOURCE = "liuchangchxy/vibe-coding-control-plane";

test("public VCCP revision reads use credential-free Git while Consumer access stays App-scoped", async () => {
  const calls = [];
  const revision = "a".repeat(40);
  const client = new AppGitHubClient({
    runtimeConfig: { github_app: {}, target_repository: CONSUMER },
    publicFetch: async () => assert.fail("public reads must not depend on API rate limits"),
    gitRunner: (command, args) => {
      calls.push({ command, args });
      if (args.includes("ls-remote")) {
        return { status: 0, stdout: `${revision}\trefs/heads/main\n`, stderr: "" };
      }
      if (args.includes("cat-file")) return { status: 0, stdout: "", stderr: "" };
      return { status: 1, stdout: "", stderr: "unexpected git operation" };
    },
  });

  const branch = await client.getBranch(SOURCE, "main");
  const commit = await client.getCommit(SOURCE, revision);

  assert.equal(branch.commit.sha, revision);
  assert.equal(commit.sha, revision);
  assert.equal(calls.length, 2);
  assert.ok(calls.every(({ command, args }) => command === "git" && args.includes("-c")
    && args.includes("credential.helper=")));
  assert.ok(calls.some(({ args }) => args.includes("ls-remote")));
  assert.ok(calls.some(({ args }) => args.includes("cat-file")));
});
