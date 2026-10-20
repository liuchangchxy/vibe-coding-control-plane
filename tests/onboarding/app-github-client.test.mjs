import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import { AppGitHubClient } from "../../lib/onboarding/app-github-client.mjs";

const CONSUMER = "liuchangchxy/dayspark";
const SOURCE = "liuchangchxy/vibe-coding-control-plane";

test("public VCCP revision reads use credential-free Git while Consumer access stays App-scoped", async () => {
  const calls = [];
  const revision = "a".repeat(40);
  const client = new AppGitHubClient({
    runtimeConfig: { github_app: {}, target_repository: CONSUMER },
    publicFetch: async () => assert.fail("public reads must not depend on API rate limits"),
    gitRunner: (command, args, options) => {
      calls.push({ command, args, options });
      if (args.includes("ls-remote")) {
        return { status: 0, stdout: `${revision}\trefs/heads/main\n`, stderr: "" };
      }
      if (args.includes("fetch")) return { status: 0, stdout: "", stderr: "" };
      if (args.includes("cat-file")) return { status: 0, stdout: "", stderr: "" };
      return { status: 1, stdout: "", stderr: "unexpected git operation" };
    },
  });

  const branch = await client.getBranch(SOURCE, "main");
  const commit = await client.getCommit(SOURCE, revision);

  assert.equal(branch.commit.sha, revision);
  assert.equal(commit.sha, revision);
  assert.equal(calls.length, 3);
  assert.ok(calls.every(({ command, args }) => command === "git" && args.includes("-c")
    && args.includes("credential.helper=")));
  assert.ok(calls.every(({ options }) => options.env.GIT_CONFIG_NOSYSTEM === "1"
    && options.env.GIT_CONFIG_GLOBAL === (process.platform === "win32" ? "NUL" : os.devNull)));
  assert.ok(calls.some(({ args }) => args.includes("ls-remote")));
  assert.ok(calls.some(({ args }) => args.includes("fetch")));
  assert.ok(calls.some(({ args }) => args.includes("cat-file")));
});

test("a locally cached commit is rejected when the public source cannot provide that revision", async () => {
  const revision = "b".repeat(40);
  const client = new AppGitHubClient({
    runtimeConfig: { github_app: {}, target_repository: CONSUMER },
    gitRunner: (_command, args) => args.includes("fetch")
      ? { status: 1, stdout: "", stderr: "remote did not provide requested revision" }
      : { status: 0, stdout: "", stderr: "" },
  });

  assert.equal(await client.getCommit(SOURCE, revision), null);
});

test("createRef forwards narrowly requested permissions to the App bridge", async () => {
  let request;
  const client = new AppGitHubClient({ runtimeConfig: { github_app: {}, target_repository: CONSUMER } });
  client.request = async (...args) => { request = args; return { ref: "created" }; };

  await client.createRef(CONSUMER, { ref: "refs/heads/codex-vccp-deploy-plan", sha: "abc" }, { workflows: "write" });

  assert.deepEqual(request, ["createRef", CONSUMER, {
    ref: { ref: "refs/heads/codex-vccp-deploy-plan", sha: "abc" },
    additional_permissions: { workflows: "write" },
  }]);
});

test("public Git reads use the Windows NUL device for an empty global config", () => {
  const originalPlatform = process.platform;
  let env;
  try {
    Object.defineProperty(process, "platform", { value: "win32", configurable: true });
    const client = new AppGitHubClient({
      runtimeConfig: { github_app: {} },
      gitRunner: (_command, _args, options) => { env = options.env; return { status: 0, stdout: "", stderr: "" }; },
    });
    client.publicGit(["ls-remote"]);
    assert.equal(env.GIT_CONFIG_GLOBAL, "NUL");
  } finally {
    Object.defineProperty(process, "platform", { value: originalPlatform, configurable: true });
  }
});
