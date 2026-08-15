import { INTERACTIVE_NAMESPACE } from "./interactive.js";


const REVIEW_ID = /^\d+$/;


export function createInboundController({ config, runWorkflow, scheduleJobs, removeJobs }) {
  const allowedSenders = new Set(config.allowedSenderIds.map(String));
  const stateChangeSenders = new Set(
    (config.stateChangeSenderIds ?? config.allowedSenderIds).map(String),
  );
  const groupId = String(config.groupId);
  const expectedOrigin = `telegram:${groupId}`;
  const approvalsInFlight = new Set();

  return {
    async handle(ctx) {
      if (!isTargetGroup(ctx, groupId, expectedOrigin)) {
        return null;
      }
      const parsed = parseCommand(ctx, config.botUsername);
      const source = resolveDmrSource(ctx);
      const explicitlyMentioned = ctx.ExplicitlyMentionedBot === true
        || parsed.addressedSlash
        || rawMentionsBot(ctx.RawBody, config.botUsername);
      const wantsRun = parsed.command === "run post-site";
      const wantsReview = source && (explicitlyMentioned || wantsRun);
      if (!wantsRun && !wantsReview) {
        return null;
      }
      if (!allowedSenders.has(String(ctx.SenderId ?? ""))) {
        return "Not authorized.";
      }
      if (!source) {
        return (
          "Ready for the Daily Meeting Report. Paste it, then reply to that DMR "
          + `and tag @${config.botUsername}.`
        );
      }
      const result = await runWorkflow("review", {
        group_id: groupId,
        sender_id: String(ctx.SenderId),
        source_text: source,
      });
      return reviewReplyPayload(result);
    },

    async handleInteractive(ctx) {
      if (!isAuthorizedCallbackContext(ctx, groupId, stateChangeSenders)) {
        if (isTargetCallbackContext(ctx, groupId)) {
          await ctx.respond.reply({ text: "Not authorized for post-site scheduling." });
        }
        return { handled: true };
      }
      const parsed = parseInteractivePayload(ctx.callback?.payload);
      if (parsed === null) {
        await ctx.respond.reply({ text: "This post-site review is no longer active." });
        return { handled: true };
      }
      if (parsed.action === "approve") {
        if (approvalsInFlight.has(parsed.reviewId)) {
          await ctx.respond.reply({ text: "This schedule approval is already in progress." });
          return { handled: true };
        }
        approvalsInFlight.add(parsed.reviewId);
        try {
          const identity = {
            review_id: parsed.reviewId,
            group_id: groupId,
            sender_id: String(ctx.senderId),
          };
          const prepared = await runWorkflow("prepare", identity);
          if (prepared.ok !== true || !Array.isArray(prepared.jobs)) {
            return await respondToWorkflowFailure(ctx, prepared);
          }
          let jobIds;
          try {
            jobIds = await scheduleJobs(prepared.jobs);
          } catch {
            await runWorkflow("rollback", identity).catch(() => undefined);
            await ctx.respond.reply({
              text: "Scheduling failed safely. Nothing was approved; you can try again.",
            });
            return { handled: true };
          }
          const committed = await runWorkflow("commit", { ...identity, job_ids: jobIds });
          if (committed.ok !== true) {
            await removeJobs(jobIds);
            await runWorkflow("rollback", identity).catch(() => undefined);
            return await respondToWorkflowFailure(ctx, committed);
          }
          // The schedule is already durable at this point. Telegram/OpenClaw can
          // occasionally apply an edit and then reject the response promise. Do
          // not turn that cosmetic cleanup failure into a false scheduling error.
          await settleCommittedUi(ctx, committed.reply);
          return { handled: true };
        } finally {
          approvalsInFlight.delete(parsed.reviewId);
        }
      }

      const result = await runWorkflow(parsed.action, {
        review_id: parsed.reviewId,
        group_id: groupId,
        sender_id: String(ctx.senderId),
      });
      if (result.ok === true) {
        await ctx.respond.clearButtons();
        await ctx.respond.editMessage({ text: result.reply });
        return { handled: true };
      }
      return respondToWorkflowFailure(ctx, result);
    },
  };
}


async function respondToWorkflowFailure(ctx, result) {
  if (result?.terminal === true) {
    await ctx.respond.clearButtons();
  }
  await ctx.respond.reply({
    text: typeof result?.reply === "string"
      ? result.reply
      : "post-site could not complete this request safely.",
  });
  return { handled: true };
}


async function settleCommittedUi(ctx, text) {
  try {
    await ctx.respond.clearButtons();
  } catch {
    // Button cleanup is best-effort after the database commit and cron creation.
  }
  try {
    await ctx.respond.editMessage({ text });
  } catch {
    // The edit may already be visible even when the adapter rejects afterward.
    // Never emit a contradictory failure after a successful durable commit.
  }
}


function reviewReplyPayload(result) {
  if (!result || typeof result !== "object" || typeof result.reply !== "string") {
    throw new TypeError("post-site review response is invalid");
  }
  const reviewId = String(result.review_id ?? "");
  if (result.ok !== true || result.can_approve !== true || !REVIEW_ID.test(reviewId)) {
    return result.reply;
  }
  return {
    text: result.reply,
    channelData: {
      telegram: {
        buttons: [
          [{
            text: "✅ Approve Schedule",
            callback_data: `${INTERACTIVE_NAMESPACE}:approve:${reviewId}`,
          }],
          [
            {
              text: "✏️ Make Changes",
              callback_data: `${INTERACTIVE_NAMESPACE}:revise:${reviewId}`,
            },
            {
              text: "❌ Cancel",
              callback_data: `${INTERACTIVE_NAMESPACE}:cancel:${reviewId}`,
            },
          ],
        ],
      },
    },
  };
}


function parseInteractivePayload(value) {
  const match = String(value ?? "").match(/^(approve|revise|cancel):(\d+)$/);
  return match ? { action: match[1], reviewId: match[2] } : null;
}


function isTargetGroup(ctx, groupId, expectedOrigin) {
  const channel = String(ctx.Provider ?? ctx.Surface ?? "").toLowerCase();
  const origin = String(ctx.OriginatingTo ?? "");
  return channel === "telegram"
    && ctx.ChatType === "group"
    && String(ctx.ChatId ?? "") === groupId
    && (!origin || origin === expectedOrigin);
}


function isTargetCallbackContext(ctx, groupId) {
  return ctx?.channel === "telegram"
    && ctx.isGroup === true
    && String(ctx.callback?.chatId ?? "") === groupId;
}


function isAuthorizedCallbackContext(ctx, groupId, allowedSenders) {
  return isTargetCallbackContext(ctx, groupId)
    && ctx.auth?.isAuthorizedSender === true
    && allowedSenders.has(String(ctx.senderId ?? ""));
}


function resolveDmrSource(ctx) {
  for (const value of [ctx.ReplyToBody, ctx.RawBody, ctx.CommandBody, ctx.BodyForCommands]) {
    const text = String(value ?? "").trim();
    if (/daily meeting report/i.test(text) && text.includes("📍")) {
      return text;
    }
  }
  return "";
}


function rawMentionsBot(value, botUsername) {
  const escaped = String(botUsername).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`@${escaped}(?=$|\\s)`, "i").test(String(value ?? ""));
}


function parseCommand(ctx, botUsername) {
  const rawBody = String(ctx.RawBody ?? "").trim();
  const escaped = String(botUsername).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const addressedSlash = new RegExp(`^/run_post_site@${escaped}\\s*$`, "i").test(rawBody);
  const source = ctx.CommandBody ?? ctx.BodyForCommands ?? rawBody;
  const normalized = String(source)
    .replace(new RegExp(`@${escaped}(?=$|\\s)`, "ig"), " ")
    .trim()
    .replace(/[-_\s]+/g, " ")
    .toLowerCase();
  const command = normalized === "/run post site" || normalized === "run post site"
    ? "run post-site"
    : normalized;
  return { command, addressedSlash };
}
