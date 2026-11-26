# Gemini CLI

Gemini CLI has hooks, so each check runs natively through `adapters/shim`. The Gemini CLI docs carry a banner saying it was replaced by Antigravity CLI on 18 June 2026. The hook and context pages below still document it. Check which one you run.

**Setup.** Put `adapters/` and the plugin folders you want under one directory and set `STEERING_HOME` to it. Merge `adapters/gemini/<plugin>.settings.json` into `.gemini/settings.json` (project), `~/.gemini/settings.json` (user) or `/etc/gemini-cli/settings.json` (system).

| | |
|---|---|
| Rule text | Paste `steering/rulings/<plugin>.md` into `GEMINI.md` (`~/.gemini/GEMINI.md` or a project file). To share one `AGENTS.md`, set `{"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}}`. `@file.md` imports work. |
| Skill | `SKILL.md` folders in `.gemini/skills/` or `.agents/skills/` (workspace), `~/.gemini/skills/` or `~/.agents/skills/` (user). `gemini skills install <git url> --path <subdir>` installs from a repository. |
| Events used | `BeforeTool`, `AfterTool`, `BeforeAgent`, `AfterAgent`. Others: `SessionStart`, `SessionEnd`, `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `PreCompress`, `Notification`. |
| Input | JSON on stdin: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `timestamp`, and for tool events `tool_name` and `tool_input` (`tool_response` on `AfterTool`). |
| Matchers | A regex on the tool name for tool events. The shell tool is `run_shell_command`. File writes use `write_file` and `replace`. |
| Blocking | Exit 2 blocks the action and sends stderr as the reason. Exit 0 stdout is parsed as JSON (`decision`, `reason`, `systemMessage`, `hookSpecificOutput.additionalContext`). Other exit codes warn and continue. |

**Context output.** Because stdout at exit 0 must be JSON, the shim wraps a check's plain text (the `cue` rule) as `hookSpecificOutput.additionalContext`.

**Not mapped.** The dispatch checks (`dispatch-model-guard`, `dispatch-isolation-guard`) have no generated Gemini config, because the docs name no subagent tool to match. If your Gemini build has one, add a `BeforeTool` entry whose matcher is that tool's name; the shim already maps `invoke_subagent`, `delegate` and `subagent` to the dispatch shape.

**Status.** The `matcher` and `hooks` field names are taken from the documented configuration schema and Claude Code's shape. They have not been run against a live Gemini CLI.

Docs: https://geminicli.com/docs/hooks/ and https://geminicli.com/docs/hooks/reference/ (events, stdin, exit codes), https://geminicli.com/docs/cli/gemini-md/ (GEMINI.md and `context.fileName`), https://geminicli.com/docs/cli/skills/ (skills).
