import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createCronScheduler } from "../scheduler.js";


const config = {
  openclawBin: "/openclaw",
  accountId: "default",
  groupId: "-1001234567890",
  agentId: "post-site",
  projectDir: "/post-site",
};


describe("post-site cron scheduler", () => {
  it("creates an exact one-shot command delivery with an idempotent key", async () => {
    const calls = [];
    const scheduler = createCronScheduler(config, async (binary, args, options) => {
      calls.push({ binary, args, options });
      return { id: "cron-1" };
    });
    const ids = await scheduler.schedule([{
      key: "post-site:17:1",
      name: "Post-site Customer Alpha",
      at: "2026-08-16T10:30:00+08:00",
      prompt: "Technician 1 — Customer Alpha @ Central District",
    }]);
    assert.deepEqual(ids, ["cron-1"]);
    assert.equal(calls[0].binary, "/openclaw");
    assert.ok(calls[0].args.includes("--delete-after-run"));
    assert.ok(calls[0].args.includes("--no-deliver"));
    assert.equal(calls[0].args[calls[0].args.indexOf("--at") + 1], "2026-08-16T10:30:00+08:00");
    const argv = JSON.parse(calls[0].args[calls[0].args.indexOf("--command-argv") + 1]);
    assert.deepEqual(argv, [
      "/openclaw", "message", "send", "--channel", "telegram", "--account", "default",
      "--target", "-1001234567890", "--message", "Technician 1 — Customer Alpha @ Central District",
    ]);
  });

  it("removes already-created jobs when a later creation fails", async () => {
    const calls = [];
    const scheduler = createCronScheduler(config, async (_binary, args) => {
      calls.push(args);
      if (args[0] === "cron" && args[1] === "rm") return { removed: true };
      if (calls.filter((value) => value[1] === "add").length === 2) throw new Error("expected");
      return { id: "cron-1" };
    });
    await assert.rejects(() => scheduler.schedule([
      { key: "one", name: "One", at: "2026-08-16T10:30:00+08:00", prompt: "One" },
      { key: "two", name: "Two", at: "2026-08-16T12:00:00+08:00", prompt: "Two" },
    ]));
    assert.ok(calls.some((args) => args[0] === "cron" && args[1] === "rm" && args[2] === "cron-1"));
  });
});
