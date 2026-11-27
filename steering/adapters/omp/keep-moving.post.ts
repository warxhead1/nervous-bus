// oh-my-pi post hook for a steering plugin. Put it in .omp/hooks/post/ (project) or ~/.omp/agent/hooks/post/.
// Runs each check through adapters/shim and turns exit code 2 into a block (pre) or extra context (post).
// Set STEERING_HOME to the folder that holds adapters/ and one folder per plugin.
import { spawnSync } from "node:child_process";

const checks: [string, string, string][] = [
  ["edit|write", "$STEERING_HOME/keep-moving/hooks/rerun-guard", "PostToolUse"],
  ["bash|read", "$STEERING_HOME/keep-moving/hooks/cue", "PostToolUse"]
];
const home = process.env.STEERING_HOME ?? "";

export default function (pi: any): void {
  pi.on("tool_result", async (event: any) => {
    for (const [pattern, script, hookEvent] of checks) {
      if (!new RegExp("^(" + pattern + ")$").test(event.toolName)) continue;
      const r = spawnSync("python3", [home + "/adapters/shim", "--harness", "omp", "--event", hookEvent, script.replace("$STEERING_HOME", home)], {
        input: JSON.stringify({ hook_event_name: "tool_result", toolName: event.toolName, input: event.input }),
        encoding: "utf8",
      });
      if (r.status === 2) return { additionalContext: r.stderr };
    }
  });
}
