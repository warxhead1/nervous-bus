// oh-my-pi pre hook for a steering plugin. Put it in .omp/hooks/pre/ (project) or ~/.omp/agent/hooks/pre/.
// Runs each check through adapters/shim and turns exit code 2 into a block (pre) or extra context (post).
// Set STEERING_HOME to the folder that holds adapters/ and one folder per plugin.
import { spawnSync } from "node:child_process";

const checks: [string, string, string][] = [
  ["bash", "$STEERING_HOME/plain-prose/hooks/prose-guard", "PreToolUse"]
];
const home = process.env.STEERING_HOME ?? "";

export default function (pi: any): void {
  pi.on("tool_call", async (event: any) => {
    for (const [pattern, script, hookEvent] of checks) {
      if (!new RegExp("^(" + pattern + ")$").test(event.toolName)) continue;
      const r = spawnSync("python3", [home + "/adapters/shim", "--harness", "omp", "--event", hookEvent, script.replace("$STEERING_HOME", home)], {
        input: JSON.stringify({ hook_event_name: "tool_call", toolName: event.toolName, input: event.input }),
        encoding: "utf8",
      });
      if (r.status === 2) return { block: true, reason: r.stderr };
    }
  });
}
