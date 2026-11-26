# oh-my-pi

oh-my-pi has TypeScript hooks that can block a tool call. Each generated file runs the checks through `adapters/shim`.

**Setup.** Put `adapters/` and the plugin folders you want under one directory and set `STEERING_HOME` to it. Copy `adapters/omp/<plugin>.pre.ts` into `.omp/hooks/pre/` (project) or `~/.omp/agent/hooks/pre/` (user), and `<plugin>.post.ts` into the matching `post/` folder. A file placed directly in `hooks/` is not discovered. The files need `python3` on the PATH and the checks' stdlib only.

| | |
|---|---|
| Rule text | oh-my-pi discovers `AGENTS.md` in the workspace and reads rules from other tools' formats. Paste `steering/rulings/<plugin>.md` into `AGENTS.md`. |
| Skill | One level deep: `<skills-root>/<skill-name>/SKILL.md`, with `name` and `description` in the frontmatter. A skill nested one level deeper is not found. |
| Events used | `tool_call` (before a tool runs) and `tool_result` (after). Others: `session_start`, `before_agent_start`, `agent_start`, `agent_end`, `turn_start`, `turn_end`, `context`. |
| Input | The handler receives `event.toolName` and `event.input` (a normalized view of the arguments). `tool_result` also carries `content`, `details` and `isError`. The shim maps `bash` to Bash and `edit` to Edit, and renames `path` to `file_path`. |
| Blocking | Return `{ block: true, reason }` from a `tool_call` handler. A handler that throws also blocks. `tool_result` can return `{ additionalContext }` or replace `content`. It cannot undo the call. |

**Not mapped.** `cue` (`UserPromptSubmit`) and `visual-proof-guard` (`Stop`) have no generated file. `before_agent_start` and `agent_end` exist, but the hooks page gives no return shape for them. The dispatch checks have no generated file either, because the docs name no subagent tool. Where a check has no hook point here, run it from a pre-commit hook or CI wrapper that feeds it the same JSON, or paste the rule text into `AGENTS.md`.

**Status.** The factory shape (`export default function (pi) { pi.on(...) }`) comes from the legacy `HookAPI` example in the hooks page, which recommends Extensions for new work. The files have not been run against a live oh-my-pi, and `write` as a tool name is unconfirmed.

Docs: https://github.com/can1357/oh-my-pi (overview), https://github.com/can1357/oh-my-pi/blob/main/docs/hooks.md (hooks), https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md (skills).
