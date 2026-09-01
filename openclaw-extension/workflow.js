import { spawn } from "node:child_process";


const MAX_OUTPUT_BYTES = 1024 * 1024;


export class WorkflowInvocationError extends Error {
  constructor(message = "post-site command failed") {
    super(message);
    this.name = "WorkflowInvocationError";
  }
}


export function createWorkflowRunner(config, spawnImpl = spawn) {
  return async (command, payload) => runChild({
    spawnImpl,
    binary: config.pythonBin,
    args: ["-m", "postsite.openclaw_bridge", command],
    input: JSON.stringify(payload),
    timeoutMs: config.workflowTimeoutMs ?? 30000,
    cwd: config.projectDir,
    env: {
      ...process.env,
      POSTSITE_DB: config.dbPath,
      TIMEZONE: config.timezone,
      PYTHONDONTWRITEBYTECODE: "1",
    },
  });
}


function runChild({ spawnImpl, binary, args, input, timeoutMs, cwd, env }) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnImpl(binary, args, {
        cwd,
        env,
        shell: false,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch {
      reject(new WorkflowInvocationError());
      return;
    }
    let stdout = Buffer.alloc(0);
    let stderrBytes = 0;
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        // Ignore termination races.
      }
      finish(() => reject(new WorkflowInvocationError("post-site command timed out")));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      stdout = Buffer.concat([stdout, Buffer.from(chunk)]);
      if (stdout.length > MAX_OUTPUT_BYTES) {
        try {
          child.kill("SIGKILL");
        } catch {
          // Ignore termination races.
        }
        finish(() => reject(new WorkflowInvocationError()));
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
        finish(() => reject(new WorkflowInvocationError()));
      }
    });
    child.on("error", () => finish(() => reject(new WorkflowInvocationError())));
    child.on("close", (_code, signal) => {
      if (settled) return;
      if (signal) {
        finish(() => reject(new WorkflowInvocationError()));
        return;
      }
      let value;
      try {
        value = JSON.parse(stdout.toString("utf8"));
      } catch {
        finish(() => reject(new WorkflowInvocationError()));
        return;
      }
      if (!value || typeof value !== "object" || typeof value.reply !== "string") {
        finish(() => reject(new WorkflowInvocationError()));
        return;
      }
      finish(() => resolve(value));
    });
    try {
      child.stdin.end(`${input}\n`);
    } catch {
      finish(() => reject(new WorkflowInvocationError()));
    }
  });
}
