import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createInboundController } from "../controller.js";


const GROUP = "-1001234567890";
const OWNER = "123456789";
const REVIEW_ID = "17";
const DMR = "📋 Daily Meeting Report — 2026-08-16 (Sun)\n1. Customer Alpha - 10:30am\n📍 Central District | Servicing";


function context(overrides = {}) {
  return {
    Provider: "telegram",
    Surface: "telegram",
    ChatType: "group",
    ChatId: GROUP,
    OriginatingTo: `telegram:${GROUP}`,
    SenderId: OWNER,
    ExplicitlyMentionedBot: false,
    RawBody: "run post-site",
    CommandBody: "run post-site",
    ...overrides,
  };
}


function harness() {
  const calls = { workflow: [], schedule: [], remove: [], responses: [] };
  const runWorkflow = async (command, payload) => {
    calls.workflow.push({ command, payload });
    if (command === "review") {
      return { ok: true, reply: "POST-SITE SCHEDULE", review_id: REVIEW_ID, can_approve: true };
    }
    if (command === "prepare") {
      return { ok: true, reply: "Preparing", review_id: REVIEW_ID, jobs: [{ key: "one" }] };
    }
    if (command === "commit") {
      return { ok: true, reply: "APPROVED", review_id: REVIEW_ID };
    }
    return { ok: true, reply: command.toUpperCase(), review_id: REVIEW_ID };
  };
  const controller = createInboundController({
    config: {
      groupId: GROUP,
      allowedSenderIds: [OWNER],
      stateChangeSenderIds: [OWNER],
      botUsername: "FieldOpsBot",
    },
    runWorkflow,
    scheduleJobs: async (jobs) => {
      calls.schedule.push(jobs);
      return ["cron-1"];
    },
    removeJobs: async (ids) => calls.remove.push(ids),
  });
  const callbackContext = (action = "approve", overrides = {}) => ({
    channel: "telegram",
    isGroup: true,
    senderId: OWNER,
    auth: { isAuthorizedSender: true },
    callback: { chatId: GROUP, payload: `${action}:${REVIEW_ID}` },
    respond: {
      reply: async (payload) => calls.responses.push({ method: "reply", payload }),
      clearButtons: async () => calls.responses.push({ method: "clearButtons" }),
      editMessage: async (payload) => calls.responses.push({ method: "editMessage", payload }),
    },
    ...overrides,
  });
  return { controller, calls, callbackContext };
}


describe("post-site inbound controller", () => {
  it("arms naturally without discussing bot tokens or cron", async () => {
    const { controller } = harness();
    const result = await controller.handle(context());
    assert.match(result, /Ready for the Daily Meeting Report/);
    assert.doesNotMatch(result, /token|cron|poller/i);
  });

  it("reviews a replied-to DMR when the bot is tagged", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      RawBody: "@FieldOpsBot",
      CommandBody: "@FieldOpsBot",
      ReplyToBody: DMR,
      ExplicitlyMentionedBot: true,
    }));
    assert.equal(calls.workflow[0].command, "review");
    assert.equal(calls.workflow[0].payload.source_text, DMR);
    assert.equal(result.text, "POST-SITE SCHEDULE");
    assert.deepEqual(result.channelData.telegram.buttons, [
      [{ text: "✅ Approve Schedule", callback_data: "post-site:approve:17" }],
      [
        { text: "✏️ Make Changes", callback_data: "post-site:revise:17" },
        { text: "❌ Cancel", callback_data: "post-site:cancel:17" },
      ],
    ]);
  });

  it("creates jobs only after approval and commits their exact IDs", async () => {
    const { controller, calls, callbackContext } = harness();
    await controller.handleInteractive(callbackContext());
    assert.deepEqual(calls.workflow.map((call) => call.command), ["prepare", "commit"]);
    assert.deepEqual(calls.schedule, [[{ key: "one" }]]);
    assert.deepEqual(calls.workflow[1].payload.job_ids, ["cron-1"]);
    assert.deepEqual(calls.responses, [
      { method: "clearButtons" },
      { method: "editMessage", payload: { text: "APPROVED" } },
    ]);
  });

  it("does not report failure when Telegram rejects after applying the approval edit", async () => {
    const { controller, calls, callbackContext } = harness();
    const ctx = callbackContext("approve");
    ctx.respond.editMessage = async (payload) => {
      calls.responses.push({ method: "editMessage", payload });
      throw new Error("adapter rejected after Telegram accepted the edit");
    };

    const result = await controller.handleInteractive(ctx);

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(calls.workflow.map((call) => call.command), ["prepare", "commit"]);
    assert.deepEqual(calls.responses, [
      { method: "clearButtons" },
      { method: "editMessage", payload: { text: "APPROVED" } },
    ]);
  });

  it("rolls the draft back and preserves buttons when scheduling fails", async () => {
    const calls = { workflow: [], responses: [] };
    const controller = createInboundController({
      config: { groupId: GROUP, allowedSenderIds: [OWNER], botUsername: "FieldOpsBot" },
      runWorkflow: async (command) => {
        calls.workflow.push(command);
        return command === "prepare"
          ? { ok: true, jobs: [{ key: "one" }] }
          : { ok: true, reply: "rolled back" };
      },
      scheduleJobs: async () => { throw new Error("expected"); },
      removeJobs: async () => undefined,
    });
    await controller.handleInteractive({
      channel: "telegram",
      isGroup: true,
      senderId: OWNER,
      auth: { isAuthorizedSender: true },
      callback: { chatId: GROUP, payload: `approve:${REVIEW_ID}` },
      respond: {
        reply: async (payload) => calls.responses.push(payload),
        clearButtons: async () => calls.responses.push("cleared"),
      },
    });
    assert.deepEqual(calls.workflow, ["prepare", "rollback"]);
    assert.deepEqual(calls.responses, [{
      text: "Scheduling failed safely. Nothing was approved; you can try again.",
    }]);
  });

  it("rejects unauthorized review and button actions", async () => {
    const { controller, calls, callbackContext } = harness();
    assert.equal(await controller.handle(context({ SenderId: "999" })), "Not authorized.");
    await controller.handleInteractive(callbackContext("approve", { senderId: "999" }));
    assert.equal(calls.workflow.length, 0);
    assert.deepEqual(calls.responses, [{
      method: "reply",
      payload: { text: "Not authorized for post-site scheduling." },
    }]);
  });

  it("handles revise and cancel without scheduling", async () => {
    for (const action of ["revise", "cancel"]) {
      const { controller, calls, callbackContext } = harness();
      await controller.handleInteractive(callbackContext(action));
      assert.deepEqual(calls.workflow.map((call) => call.command), [action]);
      assert.deepEqual(calls.schedule, []);
      assert.deepEqual(calls.responses.map((item) => item.method), ["clearButtons", "editMessage"]);
    }
  });
});
