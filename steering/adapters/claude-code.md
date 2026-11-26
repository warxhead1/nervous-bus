# Claude Code

The checks were written for this harness, so no shim is needed.

**Plugin (easiest).** See [the steering README](../README.md#take-just-one-plugin). The plugin wires the hooks itself.

**Without the plugin.** Put the plugin folders under one directory, set `STEERING_HOME` to it, and merge `adapters/claude-code/<plugin>.settings.json` into `.claude/settings.json` or `~/.claude/settings.json`. Each file is the plugin's own `hooks/hooks.json` with `${CLAUDE_PLUGIN_ROOT}` replaced by `$STEERING_HOME/<plugin>`.

| | |
|---|---|
| Rule text | Paste `steering/rulings/<plugin>.md` into `CLAUDE.md`, or `@`-import it. |
| Skill | `.claude/skills/<skill>/SKILL.md` or `~/.claude/skills/`. |
| Events used | `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`. |
| Input | One JSON object on stdin: `session_id`, `hook_event_name`, `tool_name`, `tool_input`, and for Stop `transcript_path`. |
| Blocking | Exit 2 on `PreToolUse` blocks the call and shows stderr to the agent. On `PostToolUse` the write has happened, so exit 2 reports. On `Stop` it sends the agent back once. |

Docs: https://code.claude.com/docs/en/hooks (hook events and exit codes), https://code.claude.com/docs/en/plugins/install.md (plugin install).
