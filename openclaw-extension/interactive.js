export const INTERACTIVE_NAMESPACE = "post-site";


export function registerPostSiteInteractiveHandler(api, controller) {
  api.registerInteractiveHandler({
    channel: "telegram",
    namespace: INTERACTIVE_NAMESPACE,
    handler: async (ctx) => {
      try {
        return await controller.handleInteractive(ctx);
      } catch {
        api.logger.warn?.("post-site interactive request failed safely");
        try {
          await ctx.respond.reply({
            text: "post-site could not complete this request safely.",
          });
        } catch {
          // Telegram has already acknowledged the callback. Contain reply failures.
        }
        return { handled: true };
      }
    },
  });
}
