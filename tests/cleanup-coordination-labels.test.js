const test = require("node:test");
const assert = require("node:assert/strict");
const cleanup = require("../scripts/cleanup-coordination-labels");

test("removes present active labels and leaves unrelated labels alone", async () => {
  const removed = [];
  const result = await cleanup(
    { labels: [{ name: "agent-ready" }, { name: "frozen-spec" }, { name: "bug" }] },
    async (name) => removed.push(name),
  );
  assert.deepEqual(removed, ["agent-ready"]);
  assert.deepEqual(result, { preservedTerminalState: false, removed: ["agent-ready"] });
});

test("removes active labels while preserving terminal-state evidence", async () => {
  const removed = [];
  const result = await cleanup(
    {
      labels: [
        { name: "agent-working" },
        { name: "changes-requested" },
        { name: "infra-blocked" },
        { name: "needs-human" },
      ],
    },
    async (name) => removed.push(name),
  );
  assert.deepEqual(removed, ["agent-working", "changes-requested"]);
  assert.deepEqual(result, {
    preservedTerminalState: true,
    removed: ["agent-working", "changes-requested"],
  });
});

test("does not call the remover when no active labels are present", async () => {
  let calls = 0;
  const result = await cleanup({ labels: [{ name: "needs-human" }] }, async () => calls++);
  assert.equal(calls, 0);
  assert.deepEqual(result, { preservedTerminalState: true, removed: [] });
});

test("rejects malformed inputs before attempting cleanup", async () => {
  await assert.rejects(cleanup({ labels: null }, async () => {}), TypeError);
  await assert.rejects(cleanup({ labels: [] }, null), TypeError);
});
