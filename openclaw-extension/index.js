import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { createInboundController } from "./controller.js";
import { registerPostSiteInteractiveHandler } from "./interactive.js";
import { createCronScheduler } from "./scheduler.js";
import { createWorkflowRunner } from "./workflow.js";


export default definePluginEntry({
  id: "post-site-closure-inbound",
  name: "post-site closure inbound",
  description: "Deterministic Telegram review, approval, and scheduling bridge for post-site closure.",
  register(api) {
    const config = resolveConfig(api.pluginConfig);
    if (config === null) {
      api.logger.info?.("post-site closure inbound is installed but not configured");
      return;
    }
    const workflow = createWorkflowRunner(config);
    const scheduler = createCronScheduler(config);
    const controller = createInboundController({
      config,
      runWorkflow: workflow,
      scheduleJobs: (jobs) => scheduler.schedule(jobs),
      removeJobs: (jobIds) => scheduler.remove(jobIds),
    });

    registerPostSiteInteractiveHandler(api, controller);

    api.on("reply_dispatch", async (event, hookContext) => {
      let reply;
      try {
        reply = await controller.handle(event.ctx);
      } catch {
        api.logger.warn?.("post-site inbound request failed safely");
        reply = "post-site could not complete this request safely.";
      }
      if (reply === null) {
        return;
      }
      const payload = typeof reply === "string" ? { text: reply } : reply;
      const queuedFinal = hookContext.dispatcher.sendFinalReply(payload);
      hookContext.recordProcessed("completed", { reason: "post_site_inbound" });
      hookContext.markIdle("message_completed");
      return {
        handled: true,
        queuedFinal: Boolean(queuedFinal),
        counts: hookContext.dispatcher.getQueuedCounts(),
      };
    }, { timeoutMs: 120000 });
  },
});


function resolveConfig(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const required = [
    "groupId",
    "botUsername",
    "pythonBin",
    "projectDir",
    "dbPath",
    "openclawBin",
  ];
  if (required.some((name) => typeof value[name] !== "string" || !value[name].trim())) {
    return null;
  }
  const allowedSenderIds = normalizeSenderIds(value.allowedSenderIds);
  const stateChangeSenderIds = value.stateChangeSenderIds === undefined
    ? allowedSenderIds
    : normalizeSenderIds(value.stateChangeSenderIds);
  if (allowedSenderIds === null || stateChangeSenderIds === null) {
    return null;
  }
  return {
    ...value,
    groupId: value.groupId.trim(),
    botUsername: value.botUsername.replace(/^@/, "").trim(),
    pythonBin: value.pythonBin.trim(),
    projectDir: value.projectDir.trim(),
    dbPath: value.dbPath.trim(),
    openclawBin: value.openclawBin.trim(),
    allowedSenderIds,
    stateChangeSenderIds,
    agentId: String(value.agentId ?? "post-site"),
    accountId: String(value.accountId ?? "default"),
    timezone: String(value.timezone ?? "Asia/Singapore"),
  };
}


function normalizeSenderIds(value) {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }
  const normalized = value.map((item) => String(item).trim()).filter(Boolean);
  return normalized.length ? normalized : null;
}
