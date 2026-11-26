# Codex CLI

Codex has hooks, so each check runs natively through `adapters/shim`. The shim rewrites Codex's event to the shape the checks read and passes the exit code through.

**Setup.** Put `adapters/` and the plugin folders you want under one directory and set `STEERING_HOME` to it. Merge `adapters/codex/<plugin>.hooks.json` into `$CODEX_HOME/hooks.json` (user) or `.codex/hooks.json` (trusted project). Codex asks you to review and trust hooks before they run.

| | |
|---|---|
| Rule text | Paste `steering/rulings/<plugin>.md` into `~/.codex/AGENTS.md` or the repo's `AGENTS.md`. Codex reads at most 32 KiB across the files it joins. |
| Skill | A folder holding `SKILL.md` (with `name` and `description`) in `.agents/skills/` in the repo or `~/.agents/skills/`. |
| Events used | `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`. Codex also has `SessionStart`, `PermissionRequest`, `SubagentStart` and others. |
| Input | One JSON object on stdin: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, and for tool events `tool_name` and `tool_input`. |
| Matchers | A regex on the tool name. `Bash` is the shell. `apply_patch` is the edit tool and also matches `Edit` and `Write`. `spawn_agent` also matches `Agent`. |
| Blocking | `PreToolUse` and `UserPromptSubmit`: exit 2 with the reason on stderr blocks. `PostToolUse`: the command has run, and a block replaces the tool result with the feedback. `Stop`: a block tells Codex to keep going. |

**Edit tool.** Codex edits through `apply_patch`, whose input is a patch, not `file_path` and `content`. The shim reads the file names from the patch (`*** Update File: path`) and runs the edit checks as a Write of each file as it is on disk after the edit. That counts old markers in the file as well, so `unfinished-guard` and `vacuous-test-guard` may report more than the edit added.

**Status.** The config shape follows the documented structure (event, then matcher group, then handlers) with Claude Code's field names. It has not been run against a live Codex.

Docs: https://learn.chatgpt.com/docs/hooks (hooks, also at https://developers.openai.com/codex/hooks), https://learn.chatgpt.com/docs/agent-configuration/agents-md (AGENTS.md), https://learn.chatgpt.com/docs/build-skills (skills).
