import { spawn } from "node:child_process";


const MAX_OUTPUT_BYTES = 1024 * 1024;


export class ScheduleInvocationError extends Error {
  constructor(message = "post-site scheduling failed") {
    super(message);
    this.name = "ScheduleInvocationError";
  }
}


export function createCronScheduler(config, runCommand = defaultRunCommand) {
  const messageCommand = (prompt) => [
    config.openclawBin,
    "message",
    "send",
    "--channel",
    "telegram",
    "--account",
    config.accountId,
    "--target",
    config.groupId,
    "--message",
    prompt,
  ];

  return {
    async schedule(jobs) {
      if (!Array.isArray(jobs) || jobs.length === 0) {
        throw new ScheduleInvocationError("no schedule jobs supplied");
      }
      const created = [];
      try {
        for (const job of jobs) {
          const result = await runCommand(config.openclawBin, [
            "cron",
            "add",
            "--name",
            job.name,
            "--description",
            `Approved deterministic post-site check-in (${job.key})`,
            "--at",
            job.at,
            "--agent",
            config.agentId,
            "--command-argv",
            JSON.stringify(messageCommand(job.prompt)),
            "--command-cwd",
            config.projectDir,
            "--delete-after-run",
            "--no-deliver",
            "--declaration-key",
            job.key,
            "--json",
          ], { cwd: config.projectDir });
          const jobId = result?.id ?? result?.job?.id;
          if (typeof jobId !== "string" || !jobId) {
            throw new ScheduleInvocationError("cron job id missing");
          }
          created.push(jobId);
        }
        return created;
      } catch (error) {
        await this.remove(created);
        throw error instanceof ScheduleInvocationError
          ? error
          : new ScheduleInvocationError();
      }
    },

    async remove(jobIds) {
      for (const jobId of jobIds) {
        try {
          await runCommand(config.openclawBin, ["cron", "rm", jobId, "--json"], {
            cwd: config.projectDir,
          });
        } catch {
          // Best-effort rollback; the declaration key still prevents duplicates.
        }
      }
    },
  };
}


function defaultRunCommand(binary, args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(binary, args, {
      cwd: options.cwd,
      env: process.env,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = Buffer.alloc(0);
    let stderrBytes = 0;
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      callback();
    };
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      stdout = Buffer.concat([stdout, Buffer.from(chunk)]);
      if (stdout.length > MAX_OUTPUT_BYTES) {
        try {
          child.kill("SIGKILL");
        } catch {
          // Ignore termination races.
        }
        finish(() => reject(new ScheduleInvocationError()));
      }
    });
    child.stderr.on("data", (chunk) => {
      stderrBytes += Buffer.byteLength(chunk);
      if (stderrBytes > MAX_OUTPUT_BYTES && !settled) {
        try {
          child.kill("SIGKILL");
        } catch {
          // Ignore termination races.
        }
        finish(() => reject(new ScheduleInvocationError()));
      }
    });
    child.on("error", () => finish(() => reject(new ScheduleInvocationError())));
    child.on("close", (code, signal) => {
      if (settled) return;
      if (signal || code !== 0) {
        finish(() => reject(new ScheduleInvocationError()));
        return;
      }
      try {
        const value = JSON.parse(stdout.toString("utf8"));
        finish(() => resolve(value));
      } catch {
        finish(() => reject(new ScheduleInvocationError()));
      }
    });
  });
}
