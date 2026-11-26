# Kiro CLI

Kiro CLI (V3) has hooks, so each check runs natively through `adapters/shim`.

**Setup.** Put `adapters/` and the plugin folders you want under one directory and set `STEERING_HOME` to it. Copy `adapters/kiro/<plugin>.json` to `.kiro/hooks/<plugin>.json` (workspace) or `~/.kiro/hooks/` (global). A hook can also sit as a flat `hooks` array inside an agent profile, where the older camelCase trigger names (`preToolUse`) still work.

| | |
|---|---|
| Rule text | Save `steering/rulings/<plugin>.md` as `.kiro/steering/<plugin>.md` (or `~/.kiro/steering/`). It loads with `inclusion: always` by default. Kiro also reads `AGENTS.md`, which is always included. A custom agent loads steering only if its `resources` list it, for example `file://.kiro/steering/**/*.md`. |
| Skill | Kiro documents skills as on-demand instruction packages. Its steering page gives no folder path, so check the Kiro skills docs before copying a `SKILL.md` folder. |
| Triggers used | `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`. Others: `SessionStart`, `SessionEnd`, `PreCompact`, `PostCompact`, `PermissionRequest`. |
| Input | JSON on stdin: `hook_event_name`, `cwd`, `session_id`, and for tool events `tool_name` and `tool_input`. |
| Matchers | Tool groups `read`, `write`, `shell`, `web`, `subagent`, or a regex on the tool id. |
| Blocking | For a command hook, exit 0 puts stdout in the agent's context. Any other exit code sends stderr to the agent, and on `PreToolUse` the tool call is blocked. Default timeout is 60 seconds. The docs do not say whether `PostToolUse` or `Stop` can undo anything, so treat them as report-only. |

**Status.** The file format is the documented standalone format (`version`, `hooks[]` with `name`, `trigger`, `matcher`, `action`, `timeout`, `enabled`). It has not been run against a live Kiro CLI. The Hook types page lists the tool input shape only for MCP tools, so the shim also accepts Kiro's `execute_bash`, `fs_write` and `str_replace` names and `path`, `file_text`, `old_str` and `new_str` keys on a best-effort basis.

Docs: https://kiro.dev/docs/hooks/ and https://kiro.dev/docs/cli/v3/hooks-migration/ (format and triggers), https://kiro.dev/docs/hooks/actions/ (command actions and exit codes), https://kiro.dev/docs/hooks/types/ (trigger list), https://kiro.dev/docs/steering/ (steering and AGENTS.md).
