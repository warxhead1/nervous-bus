// nervous-bus plugin for opencode.
// Install with: python3 tools/setup.py hooks --agent opencode
//
// Publishes session lifecycle to agent.opencode.session and runs argshape live
// on bash calls (tool.execute.before / tool.execute.after). Nothing here may
// throw into opencode: every failure is swallowed.
import { spawnSync } from "node:child_process";

const SESSION_EVENTS = {
  "session.created": "started",
  "session.idle": "idle",
  "session.deleted": "ended",
};

export const NervousBus = async ({ $, directory }) => {
  const argshape = process.env.ARGSHAPE_BIN || "argshape";
  const nervous = process.env.NERVOUS_BIN || "nervous";
  const cwd = directory || process.cwd();
  const commands = new Map();

  // `$` is the Bun shell opencode hands to the plugin factory. It is not a
  // module global, so publish() closes over the factory argument.
  async function publish(channel, payload) {
    try {
      const body = JSON.stringify(payload);
      if (typeof $ === "function") {
        await $`${nervous} publish ${channel} ${body}`.quiet().nothrow();
      } else {
        spawnSync(nervous, ["publish", channel, body], { stdio: "ignore", timeout: 5000 });
      }
    } catch (_) {
      // best effort
    }
  }

  function hook(kind, ev) {
    try {
      const p = spawnSync(argshape, ["hook", kind], {
        input: JSON.stringify(ev),
        encoding: "utf8",
        timeout: 5000,
        windowsHide: true,
      });
      if (p.status !== 0 || !p.stdout || !p.stdout.trim()) return null;
      return JSON.parse(p.stdout).hookSpecificOutput || null;
    } catch (_) {
      return null;
    }
  }

  const base = (input, command) => ({
    session_id: input.sessionID,
    tool_use_id: input.callID,
    tool_name: "Bash",
    tool_input: { command },
    cwd,
  });

  return {
    event: async ({ event }) => {
      const kind = SESSION_EVENTS[event && event.type];
      if (!kind) return;
      const props = event.properties || {};
      const info = props.info || {};
      const sessionId = info.id || props.sessionID || "";
      if (!sessionId) return;
      const payload = {
        event: kind,
        session_id: sessionId,
        cwd: info.directory || cwd,
        ts: new Date().toISOString(),
      };
      if (info.parentID) payload.parent_session_id = info.parentID;
      if (info.title) payload.title = info.title;
      await publish("agent.opencode.session", payload);
    },

    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash" || !output.args || !output.args.command) return;
      const res = hook("pre", base(input, output.args.command));
      const fixed = res && res.updatedInput && res.updatedInput.command;
      if (fixed) output.args.command = fixed;
      commands.set(input.callID, output.args.command);
    },

    "tool.execute.after": async (input, output) => {
      if (input.tool !== "bash") return;
      const command = (input.args && input.args.command) || commands.get(input.callID);
      commands.delete(input.callID);
      if (!command) return;
      const exit = output.metadata && output.metadata.exit;
      const text = typeof output.output === "string" ? output.output : "";
      if (typeof exit === "number" && exit !== 0) {
        hook("fail", { ...base(input, command), error: text });
      } else {
        hook("post", { ...base(input, command), tool_response: { stdout: text } });
      }
    },
  };
};
