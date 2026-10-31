# Where coding agents keep their transcripts

Source table for `adapters/transcripts/`, the reader that turns every agent's
session files into one stream of normalized tool calls. Researched 2026-10-31:
**DISK** = checked on a Linux install, **SOURCE** = the agent's own code,
**DOCS** = official docs, **3P** = third-party reader or blog (weakest).
These tools change monthly; re-verify a row before relying on it, and update
the row when you add or fix a source.

Readable today (`transcripts sources`): see the registry in
`adapters/transcripts/sources/`. Everything else below is a work order.

## Readers in this repo

| agent | module | verified on real sessions |
|:-|:-|:-|
| Claude Code | `sources/claude.py` | yes, including `<session>/subagents/` |
| Codex CLI | `sources/codex.py` | yes, both shell-call generations |
| Gemini CLI | `sources/gemini.py` | legacy `.json` yes; `.jsonl` tool calls from a fixture only (no real `.jsonl` session with tool calls seen yet) |
| Antigravity CLI | `sources/agy.py` | yes, `brain/<id>/.system_generated/logs/transcript_full.jsonl` |
| Cursor CLI | `sources/cursor.py` | yes; the format records no tool results |
| opencode | `sources/opencode.py` | yes, SQLite read-only (`session`, `part`) |

Findings from building them, which override the research notes below:

- **Gemini CLI.** Results are at `result[0].functionResponse.response` (`output` or `error`). Shell `status` is `success` even when the program exited nonzero. The exit code is an `Exit Code: N` line in the output, and some successful calls don't have one. Denied or cancelled calls carry status `error`/`cancelled` and text such as `Tool execution denied`. JSONL metadata lines are `{"$set": {"lastUpdated": ...}}`. `~/.gemini/projects.json` (`{projects: {path: shortname}}`) maps `tmp/<name>` back to a cwd.
- **Antigravity CLI.** The steps after a `PLANNER_RESPONSE` step, up to the next planner step, are the results of its `tool_calls`, in order (the counts matched in 97% of planner steps). Most `run_command` results are `GENERIC` steps, not `RUN_COMMAND`. Exit text is `The command completed successfully`, `The command exited with code N`, or `The command failed with exit code N`. `RUNNING` marks a background task that has no result. `transcript.jsonl` is truncated (`truncated_fields`) and `transcript_full.jsonl` is not.
- **Cursor CLI.** The shell tool is `Shell` with input `{command, description}`. `tool_use` blocks have no `id`, and no results are written.
- **opencode.** A tool part's `data` is `{type: "tool", tool: "bash", state: {status, input: {command}, output, metadata: {exit, output, truncated}}}`. The database also has `session_message` and `event` tables (event-sourced, not read yet) and credential tables, which a reader must never select.

## (a) Summary table

| Agent | Linux | macOS | Windows | Format | Live hook | Evidence |
|---|---|---|---|---|---|---|
| Claude Code | `~/.claude/projects/<proj>/<session>.jsonl`, subagents in `<session>/subagents/` | same | `%USERPROFILE%\.claude` | JSONL, one file per session | PreToolUse/PostToolUse in `settings.json` | DISK + DOCS |
| Codex CLI | `$CODEX_HOME` (default `~/.codex`) `/sessions/YYYY/MM/DD/rollout-*.jsonl` | same | `dirs::home_dir()` + `.codex`, unconfirmed | JSONL | PreToolUse in `hooks.json` or `config.toml` | DISK + SOURCE + DOCS |
| Gemini CLI | `~/.gemini/tmp/<projectId>/chats/session-<ts>-<id8>.jsonl` (legacy `.json`) | same | derived `%USERPROFILE%\.gemini` | JSONL, legacy JSON | BeforeTool/AfterTool in `settings.json` | DISK + SOURCE |
| Antigravity CLI | `~/.gemini/antigravity-cli/` (`conversations/<id>.db` and `.pb`; `brain/<id>/.system_generated/logs/transcript*.jsonl`) | unknown | unknown | SQLite+protobuf, opaque `.pb`, readable JSONL | none found | DISK |
| Antigravity IDE | `~/.gemini/antigravity/` (`conversations/*.pb`; `brain/<id>/.system_generated/logs/overview.txt`) | unknown | unknown | opaque protobuf, overview text | none found | DISK |
| opencode | `~/.local/share/opencode/opencode*.db` | same | unknown | SQLite (WAL) | plugin `tool.execute.before/after` | DISK + DOCS + 3P |
| Cursor CLI | `~/.cursor/projects/<proj>/agent-transcripts/<id>/<id>.jsonl`; `~/.cursor/chats/<h>/<id>/store.db` | same | unknown | JSONL; SQLite (`meta`, `blobs`) | `~/.cursor/hooks.json`; CLI coverage partial | DISK + DOCS |
| Factory Droid | `~/.factory/` (sessions NOT on disk here) | same | `%USERPROFILE%\.factory\settings.json` (settings only) | JSONL per hook docs | `~/.factory/hooks.json` | DOCS + 3P |
| Kimi CLI | `~/.kimi/sessions/<md5(cwd)>/<id>/{context,wire}.jsonl,state.json` | same | not stated | JSONL | `[[hooks]]` in `~/.kimi/config.toml` | DOCS |
| pi | `~/.pi/agent/sessions/--<path>--/<ts>_<uuid>.jsonl` | same | not stated | JSONL tree | extension `tool_call` event | DOCS |
| Kiro CLI | `~/.kiro/` SQLite (official); `data.sqlite3` in platform data dir (3P) | unknown | unknown | SQLite | `.kiro/hooks/*.json`, global `~/.kiro/hooks/` | DOCS + 3P |
| Amp | possibly `~/.local/share/amp/threads/*.json` (3P, contested) | same | unknown | JSON | plugin `tool.call` | DOCS + 3P |
| Goose | `<data_dir>/sessions/sessions.db` | same | etcetera app strategy for `Block/goose` | SQLite | lifecycle hooks (PreToolUse etc.) | SOURCE + DOCS |
| Aider | `./.aider.chat.history.md`, `./.aider.input.history` | same | same (project-relative) | Markdown and text | none | DOCS |
| Qwen Code | `~/.qwen/tmp/<hash>/chats/` (`QWEN_HOME`) | same | unknown | unverified | `hooks` in settings, PreToolUse | SOURCE + DOCS |
| Crush | project `.crush/` db; registry `~/.local/share/crush/` | same | `%LOCALAPPDATA%\crush` (registry file only) | SQLite | none found | SOURCE + 3P |
| Copilot CLI | `~/.copilot/session-state/<id>/events.jsonl` (`COPILOT_HOME`) | same | `%USERPROFILE%\.copilot` (hooks dir) | JSONL, undocumented | `~/.copilot/hooks/*.json`, `.github/hooks/*.json` | DOCS (hooks) + 3P (store) |
| Cline CLI | `~/.cline/data/` (`CLINE_DATA_DIR`), `sessions/` and `db/` | same | not stated | SQLite + per-session folders | `~/.cline/hooks`, `.cline/hooks` | DOCS |
| Continue `cn` | `~/.continue/sessions/<id>.json` (`CONTINUE_GLOBAL_DIR`) | same | unknown | JSON per session | none found | SOURCE |
| OpenHands CLI | `~/.openhands/conversations/<id>/conversation.json` | same | not stated | JSON (SDK: `events/event-*.json`) | none found | DOCS |

## (b) Per-agent sections

### Claude Code
- **Store:** `~/.claude/projects/<cwd with non-alphanumerics replaced by '-'>/<session>.jsonl`. Subagents live at `<session>/subagents/` in the same format. Large tool outputs spill to `<session>/tool-results/`. Set-aside files are `*.orphaned-<ts>-*.jsonl` and `*.jsonl.superseded-<ts>`. (DISK, https://code.claude.com/docs/en/claude-directory.md)
- **Override:** `CLAUDE_CONFIG_DIR`. Windows is `%USERPROFILE%\.claude`. (https://code.claude.com/docs/en/settings.md)
- **Keys** (from `adapters/argshape/backtest.py:45-78`): `sessionId`, `cwd`, `message.content[]` blocks `type=tool_use` (`id`, `name`, `input.command`) and `type=tool_result` (`tool_use_id`, `is_error`, `content`). Model, timestamp, and usage keys are not read in `backtest.py`; `token_ledger.py` reads them.
- **Hooks:** PreToolUse and PostToolUse. Exit code 2 blocks; JSON can return allow, deny, or ask. (https://docs.anthropic.com/en/docs/claude-code/hooks)
- **Retention:** `cleanupPeriodDays`, default 30, minimum 1. It deletes transcripts, subagent dirs, and tool-results by age. A managed-settings value forces the sweep. `CLAUDE_CODE_SKIP_PROMPT_HISTORY` disables transcripts. `claude purge` deletes per project. `history.jsonl` is kept.
- **Gotcha:** when the sweep is paused, files persist past the cutoff.

### Codex CLI
- **Store:** `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`. `SESSIONS_SUBDIR="sessions"` at openai/codex `codex-rs/rollout/src/lib.rs:85`; `archived_sessions` at line 86. `CODEX_HOME` is resolved in `codex-rs/utils/home-dir`, falling back to `<home>/.codex`.
- **On this box (DISK):** `~/.codex/sessions` is empty. Rollouts live under Orca account homes at `~/.config/orca/codex-accounts/<uuid>/home/sessions/...`. `token_ledger.py:57-58` already globs both roots. The same session can appear under several roots, so dedupe by session id (`token_ledger.py:543-554`).
- **Keys** (`token_ledger.py:478-525`): line shape is `{timestamp, type, payload}`.
  - `session_meta`: `payload.id`, `payload.cwd`, `source`, `thread_source`, `parent_thread_id`, `cli_version`, `originator`, `model_provider`.
  - `turn_context`: `payload.model`.
  - `response_item`: `payload.type` of `function_call` (`name`, `arguments` as a JSON string, `call_id`), `custom_tool_call` (`input`), `function_call_output` or `custom_tool_call_output` (`call_id`, `output`).
  - `event_msg` with `payload.type=token_count`: `info.total_token_usage` and `info.last_token_usage` (`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`). `info` can be null.
  - Shell calls (DISK, read by `sources/codex.py`): 2026 rollouts carry `event_msg` / `item_completed` with `item.type=CommandExecution` (`command` argv list, `cwd`, `exit_code`, `aggregated_output`, `status` completed|failed). Older rollouts use `function_call` named `shell`, `shell_command`, `exec_command` or `local_shell`, paired with `function_call_output` whose text starts `Exit code: N` or is JSON with `metadata.exit_code`. A file with any CommandExecution item also holds the matching function_calls, so read one or the other, never both.
- **Hooks:** PreToolUse, PostToolUse, PermissionRequest, SessionStart/End, PreCompact/PostCompact, UserPromptSubmit, SubagentStart/Stop, Stop, Interrupt. Config is `hooks.json` or `[hooks]` in `config.toml`, under `~/.codex/` and `<repo>/.codex/` (project layer must be trusted). Stdin has `session_id`, `transcript_path` (nullable), `cwd`, `model`, `tool_name`, `tool_input`. Shell is matched as `Bash`. There is a `commandWindows` override. (https://learn.chatgpt.com/docs/hooks, redirected from developers.openai.com/codex/hooks)
- **Gotchas:** resumed or forked sessions can duplicate rollouts across roots. Subagent rollouts are marked by `thread_source=="subagent"` or a dict-valued `source`.

### Gemini CLI
- **Store (DISK):** two generations exist side by side.
  - Legacy: `~/.gemini/tmp/<64-hex projectHash>/chats/session-<date>T<hh-mm>-<id8>.json`, one JSON document with `sessionId`, `projectHash`, `startTime`, `lastUpdated`, `messages[]`. There are more than 100 such sessions, from 2025.
  - Current: `~/.gemini/tmp/<project name>/chats/session-<date>T<hh-mm>-<id8>.jsonl`. Line 1 is a header (`sessionId`, `projectHash`, `startTime`, `lastUpdated`, `kind`). Message lines have `id`, `timestamp`, `type`, `content`. Bare `{lastUpdated}` lines are metadata updates, so the file is append-only. Nine such sessions exist on this box.
- **Source** (`packages/core/src/services/chatRecordingService.ts`, via https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/services/chatRecordingService.ts): `.jsonl`; a legacy `.json` is converted on load; subagent sessions go in a subfolder named for the parent session id. `getProjectTempDir()` uses a registry-assigned short id, not a hash. The fetch summarizer inferred the `MessageRecord` field list and did not read the type definition (it is in `chatRecordingTypes.js`).
- **Message keys** (legacy file, DISK):
  - `id`, `timestamp`, `type` (`user` or `gemini`), `content`, `thoughts`, `model`.
  - `tokens{input, output, cached, thoughts, tool, total}`.
  - `toolCalls[]` with `name`, `args`, `result` (including `output`), `status`, `resultDisplay`, `displayName`.
  - Absent: cwd (derive it from the project mapping) and an explicit exit code (inspect `result`).
- **Hooks:** BeforeTool, AfterTool, BeforeAgent, AfterAgent, BeforeModel, AfterModel, BeforeToolSelection, SessionStart/End, Notification, PreCompress. Config is the `hooks` object in `settings.json`. Stdin has `session_id`, `transcript_path`, `cwd`, `timestamp`; `tool_name`, `tool_input`, `tool_response{llmContent, returnDisplay, error}`. Exit 2 or `decision:"deny"` blocks. (https://geminicli.com/docs/hooks/reference/)
- **Gotchas:**
  - `transcript_path` is unavailable at SessionStart (3P: https://mintlify.com/aannoo/hcom/integration/gemini-cli).
  - `GEMINI_CLI_HOME` was NOT found in `storage.ts`.
  - A 3P guide claims Gemini CLI sunsets 2026-06-18 with Antigravity CLI as successor. That is unconfirmed by official sources, but it matches this box having both trees.

### Antigravity (agy)
Existing reader: `adapters/reflex-recorder/agy_transcripts.py`.
- **CLI store (DISK):**
  - `~/.gemini/antigravity-cli/conversations/<id>.db` (152 present) is SQLite with protobuf blobs; `.pb` siblings are opaque.
  - NEW: `brain/<id>/.system_generated/logs/transcript.jsonl` and `transcript_full.jsonl` exist in 159 sessions (318 files).
  - Keys: `step_index`, `source` (`USER_EXPLICIT`, `MODEL`, `SYSTEM`), `type` (`USER_INPUT`, `PLANNER_RESPONSE`, `RUN_COMMAND`, `GREP_SEARCH`, `CONVERSATION_HISTORY`, ...), `status`, `created_at`, `content`. Planner steps add `thinking` and `tool_calls[{name, args}]`.
  - Shell calls use `name=run_command` with `args.CommandLine`, `args.Cwd`, `toolAction`, `toolSummary`.
  - The `RUN_COMMAND` step `content` holds timing plus a success or failure sentence and the output. `truncated_fields:["content"]` marks truncation.
  - Model and token keys are absent from the JSONL; they exist only in the `.db` (existing reader).
  - Whether a `RUN_COMMAND` result step precedes or follows its planner `tool_calls` step was NOT verified, so do not pair by adjacency without checking.
  - `transcript_full.jsonl` differs from `transcript.jsonl` by truncation. A 876-versus-98 `RUN_COMMAND` count was measured on one session, but which file each count came from was not recorded.
- **IDE store (DISK):** `~/.gemini/antigravity/conversations/*.pb` is opaque. Only `overview.txt` exists, in 4 of the brains globbed; a pattern for `transcript*.jsonl` there found none.
- **Hooks:** none found in this search. Windows and macOS paths unknown.

### opencode
- **Store (DISK):** `~/.local/share/opencode/opencode-.db` plus `-wal` and `-shm`. Note the empty channel suffix after the dash. A 3P reader says to glob `opencode*.db` because channels suffix the filename. Env overrides `OPENCODE_DB` and `OPENCODE_DATA_DIR` are claimed only by 3P Go adapters.
- **Schema:** I could not read the schema from the file. The Grep tool could not match inside the binary (it reported a binary file), so I did not extract table names. 3P (https://docs.rs/crate/ocs/latest/source/src/storage.rs) says: `session` (`id`, `directory`, `parent_id`, `title`, times), `message` (`id`, `session_id`, `time_created`, `data` JSON), `part` (JSON `data` whose `type` selects tool, text, etc.). Subagents are `session` rows with `parent_id` set.
- **Source:** `sst/opencode/.../storage/db.ts` was 404, and `anomalyco/opencode` could not be fetched, so SOURCE-level confirmation is missing.
- **Hooks:** plugin hooks `tool.execute.before`, `tool.execute.after`, `shell.env`, `event`. Plugins load from `~/.config/opencode/plugins/` and `.opencode/plugins/`, or the `plugin` array in `opencode.json`. (https://opencode.ai/docs/plugins/)
- **Windows:** unknown. 3P says `%LOCALAPPDATA%` or `%APPDATA%`; I did not confirm it.

### Cursor
- **CLI transcripts (DISK):** `~/.cursor/projects/<cwd-slug>/agent-transcripts/<id>/<id>.jsonl`. Keys: `role`, `message.content[]` blocks `type=text` or `tool_use` (`name`, `input`). No timestamp, tool-result, or token keys appeared in the first lines. The per-project dir also holds `repo.json` and `worker.log`.
- **`store.db` (DISK):** `~/.cursor/chats/<hash>/<uuid>/store.db` has tables `meta` and `blobs` only; a sibling `meta.json` has `schemaVersion`, `createdAtMs`, `hasConversation`, `title`, `updatedAtMs`. 3P (https://docs.rs/crate/agent-chorus/0.16.0/source/src/cursor_app.rs):
  - `meta` is one row of hex-encoded JSON (`agentId`, `latestRootBlobId`, `name`, `mode`, `createdAt`).
  - `blobs` is content-addressed: id is SHA-256 of data, with JSON message blobs plus protobuf DAG nodes. Reverse-engineered, so verify.
- **IDE store:** `state.vscdb` is the IDE's key-value store, not read here.
- **Hooks:** events include `preToolUse`, `postToolUse`, `postToolUseFailure`, `beforeShellExecution`, `afterShellExecution`, `sessionStart/End`, `stop`, `preCompact`, `subagentStart/Stop`. Config is `~/.cursor/hooks.json`, `<project>/.cursor/hooks.json`, and enterprise paths (`/etc/cursor/hooks.json`, `C:\ProgramData\Cursor\hooks.json`, macOS `/Library/Application Support/Cursor/hooks.json`). Common stdin fields: `conversation_id`, `model`, `workspace_roots`, `transcript_path`. The docs list `cwd`, `command`, and `duration` as hook-specific fields; `exit_code` is not a stdin field. (https://cursor.com/docs/hooks)
- **CLI coverage:** the docs name `cursor-agent` only for `workspaceOpen`. A forum thread (https://forum.cursor.com/t/cursor-cli-hooks/148511) reports the shell and file-edit hooks work in the CLI; this is user-reported and old.

### Factory Droid
- **Disk:** only `~/.factory/settings.json` exists on this box. NOT VERIFIED-ON-DISK.
- **Store:** the docs example transcript path is `.../.factory/projects/.../session.jsonl` (https://docs.factory.com/reference/hooks-reference.md). A 3P page claims `~/.factory/sessions/<path-with-dashes>/<uuid>.jsonl` plus `<uuid>.settings.json` (https://docs.entire.io/agents/factory-droid). The two disagree, and I could not settle it.
- **Hooks:** PreToolUse, PostToolUse, UserPromptSubmit, Notification, Stop, SubagentStop, PreCompact, SessionStart/End. Config is `~/.factory/hooks.json`, `.factory/hooks.json`, with fallback to the `hooks` key in `settings.json`. Stdin has `session_id`, `transcript_path`, `cwd`, `permission_mode`, `tool_name`, `tool_input`, and `tool_response` on PostToolUse.
- **Windows:** `%USERPROFILE%\.factory\settings.json` (https://docs.factory.ai/droid-cli/settings); the sessions path is unknown.

### Kimi CLI
- **Store:** `~/.kimi/sessions/<work-dir-hash>/<session-id>/{context.jsonl, wire.jsonl, state.json}`; subagents in `<session-id>/subagents/<agent_id>/`; imports in `~/.kimi/imported_sessions/`. Override `KIMI_SHARE_DIR`. Windows not stated. (https://moonshotai.github.io/kimi-cli/en/configuration/data-locations.html)
- **Two generations:** the newer Kimi Code docs describe `~/.kimi-code/` (`$KIMI_CODE_HOME`), `session_index.jsonl`, and `agents/main/wire.jsonl` (https://moonshotai.github.io/kimi-code/en/guides/sessions). Check which product is installed.
- **Keys:** field names inside `context.jsonl` and `wire.jsonl` were NOT fetched.
- **Hooks:** `[[hooks]]` entries (`event`, `matcher`, `command`, `timeout`) in `~/.kimi/config.toml`. JSON on stdin, snake_case; deny via `permissionDecision:"deny"` JSON. 13 lifecycle events including PreToolUse. (https://moonshotai.github.io/kimi-cli/en/customization/hooks.html)
- **Retention:** none; data persists until deleted manually.

### pi (badlogic/pi-mono)
- **Store:** `~/.pi/agent/sessions/--<cwd with / \ : replaced by ->--/<timestamp>_<uuid>.jsonl`. (https://pi.dev/docs/latest/session-format)
- **Format:** JSONL tree linked by `id` and `parentId`, so branches share one file. Header `type:"session"` with `id`, `cwd`, `timestamp`, `version` (3), `parentSession`. Entry types: `message`, `model_change`, `thinking_level_change`, `compaction`.
- **Message keys:**
  - Assistant: `message.role`, `api`, `provider`, `model`, `usage{input, output, cacheRead, cacheWrite, totalTokens, cost{...}}`.
  - Tool result: `role=toolResult`, `toolCallId`, `toolName`, `isError`, `content`.
  - Tool-call block names are not documented on that page.
- **Hooks:** extensions loaded from `~/.pi/agent/extensions/` and `.pi/extensions/`. A `tool_call` event can block a tool; the return value only blocks, and mutating the input in place modifies arguments (https://pi.dev/docs/latest/extensions).
- **Env override:** `PI_CODING_AGENT_DIR` is not mentioned in the fetched page. Windows not stated.

### Kiro CLI
- **Store:** official docs say a local SQLite database in `~/.kiro/`, keyed by project directory, saved every turn (https://kiro.dev/docs/cli/chat/session-management/). 3P (https://pypi.org/project/kiro-cli-history/) says `data.sqlite3` in a platform data directory for v2 and JSONL under `~/.kiro/sessions/cli/` for classic mode. Conflict unresolved; the exact path is unknown.
- **Export:** `/chat save` writes a zip; `/chat save-via-script` passes session JSON on stdin.
- **Hooks:** `.kiro/hooks/*.json` per project and `~/.kiro/hooks/` global (CLI v3). PreToolUse can block, but the exit-code contract and stdin fields were not retrievable (https://kiro.dev/docs/hooks/).
- **Retention:** none documented. Windows unknown.

### Amp (Sourcegraph)
- **Store:** the official thread docs do NOT state a local path (https://ampcode.com/docs/threads). 3P says `~/.local/share/amp/threads/*.json`, but another 3P source says current Amp is server-authoritative and does not read or write it. Treat as unresolved. Use `amp threads export T-…` for JSON.
- **Hooks:** plugins in `.amp/plugins/` and `~/.config/amp/plugins/`, run with Bun. Events: `session.start`, `tool.call`, `tool.result`, `agent.start`, `agent.end`. A `tool.call` handler can allow, reject-and-continue, or modify input, and `amp.helpers.shellCommandFromToolCall` reads the shell command. Modify on shell calls was not confirmed. (https://ampcode.com/docs/plugin-api; page was truncated at 100k chars.)

### Goose (Block)
- **Store:** `<data_dir>/sessions/sessions.db` (SQLite), built in `SessionStorage::new` in https://raw.githubusercontent.com/block/goose/main/crates/goose/src/session/session_manager.rs. `GOOSE_PATH_ROOT` (absolute) puts data under `<root>/data` (https://raw.githubusercontent.com/block/goose/main/crates/goose/src/config/paths.rs). Otherwise the `etcetera` app strategy for `Block/goose` is used, so the macOS and Windows paths are not shown in the code. 3P says Linux `~/.local/share/goose/sessions/sessions.db` and Windows `%APPDATA%/Block/goose/...`; unconfirmed.
- **Tables:** `sessions(id, name, description, session_type, working_dir, created_at, updated_at, total_tokens, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, accumulated_*, provider_name, model_config_json, goose_mode, parent_session_id, ...)`. `messages(id, message_id, session_id, role, content_json, created_timestamp, timestamp, tokens, metadata_json)`. Only the first 100k chars of the file were read, so migrations may add more.
- **Tool calls:** live inside `content_json`; the schema within was not examined.
- **Older:** pre-v1.20 per-session JSONL in `~/.goose/sessions/` (3P: https://instagit.com/block/goose/goose-sqlite-session-storage-jsonl-migration/).
- **Hooks:** lifecycle hooks following the Open Plugins hooks spec, with PreToolUse returning `decision` allow or deny, and shell-specific before/after events (https://goose-docs.ai/docs/guides/context-engineering/hooks/). Config location not read.

### Aider
- **Store:** project-relative files: `.aider.chat.history.md` (`--chat-history-file`, `AIDER_CHAT_HISTORY_FILE`), `.aider.input.history`, and `--llm-history-file` / `--analytics-log` with no default. (https://aider.chat/docs/config/options.html)
- **Format:** Markdown; runs are delimited by `# aider chat started at` headers (3P: https://docs.rs/crate/terraphim-session-analyzer/latest/source/src/connectors/aider.rs). No structured tool calls, exit codes, or tokens, and Aider has no shell tool. Reading it for failed tool calls or token use is not meaningful.
- **Hooks:** none found.

### Qwen Code
- **Store:** `getGlobalQwenDir()` is `~/.qwen` or `$QWEN_HOME`. Project dir is `<runtimeBase>/tmp/<projectHash>`, with chats under `<projectDir>/chats`. `QWEN_RUNTIME_DIR` redirects runtime output. (SOURCE: https://raw.githubusercontent.com/QwenLM/qwen-code/main/packages/core/src/config/storage.ts)
- **Format:** NOT checked. It is a Gemini CLI fork and likely shares the chat layout, but I did not verify that. Not on this box.
- **Hooks:** hooks are enabled by default, with `disableAllHooks` to turn them off; PreToolUse matches tool names (WriteFile, ReadFile, Bash). (https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks)

### Crush (Charm)
- **Store:** per-project `data_dir`, default `.crush` (3P-confirmed; the SOURCE excerpt didn't define the constant, and the `crush.db` filename wasn't found). Global registry `projects.json` under `$CRUSH_GLOBAL_DATA`, then `$XDG_DATA_HOME/crush/`, `%LOCALAPPDATA%\crush`, or `~/.local/share/crush`. (SOURCE: https://raw.githubusercontent.com/charmbracelet/crush/main/internal/config/load.go)
- **Initial migration tables:** `sessions(id, parent_session_id, title, message_count, prompt_tokens, completion_tokens, cost, ...)`, `files(...)`, `messages(id, session_id, role, parts, model, created_at, updated_at, finished_at)`. (SOURCE: `internal/db/migrations/20250424200609_initial.sql`; later migrations may differ.)
- **Gotchas:** 3P says timestamps are seconds and that Crush does not record per-message token data. Tool calls are inside `parts`. A reader has to find each project's db by walking `projects.json`.
- **Hooks:** none found.

### GitHub Copilot CLI
- **Hooks (DOCS):** `~/.copilot/hooks/*.json` (`$COPILOT_HOME`, Windows `%USERPROFILE%\.copilot\hooks\`), repo `.github/hooks/*.json`, policy `C:\ProgramData\GitHub\Copilot\policy.d\*.json`. Events include `preToolUse`, `postToolUse`, `postToolUseFailure`, `sessionStart/End`, `agentStop`, `subagentStart/Stop`, `preCompact`, `permissionRequest`. Payload has `sessionId`, `timestamp`, `cwd`, `toolName`, `toolArgs`, `toolResult`; `agentStop`, `subagentStart/Stop`, and `preCompact` also carry `transcriptPath`. Deny via JSON `permissionDecision:"deny"` or exit 2. (https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference)
- **Store (3P only):** `~/.copilot/session-state/<id>/events.jsonl` plus `workspace.yaml` (git context, cwd). The format is undocumented and readers say it is unstable. Only `assistant.message` `outputTokens` and per-model totals at shutdown were reported. (https://docs.rs/crate/toolpath-copilot/0.1.0)

### Cline CLI
- **Store (DOCS):** `~/.cline/data/` with override `CLINE_DATA_DIR`. Session folders in `~/.cline/data/sessions/`, SQLite under `~/.cline/data/db/`. Windows not stated. (https://docs.cline.bot/cline-cli/configuration)
- **Older layout (VS Code extension):** `tasks/<id>/{api_conversation_history.json, ui_messages.json, task_metadata.json}` (https://docs.cline.bot/troubleshooting/task-history-recovery.md). Do not assume the CLI uses it.
- **Hooks:** `~/.cline/hooks/`, `~/Documents/Cline/Hooks/`, `.cline/hooks/`, `CLINE_HOOKS_DIR`, `--hooks-dir`. Must be enabled with `cline config` (`hooks-enabled`). Contract not read.

### Continue CLI (`cn`)
- **Store (SOURCE):** `getSessionDir()` returns `<continueHome>/sessions` (default `~/.continue`; env `CONTINUE_GLOBAL_DIR`). File is `<sessionId>.json` with `sessionId`, `title`, `workspaceDirectory`, `history`, `usage`. (continuedev/continue `extensions/cli/src/session.ts:35-59,67,100-105`, via https://raw.githubusercontent.com/continuedev/continue/main/extensions/cli/src/session.ts)
- **Conflict:** a 2026 README mirror says `~/.continue-cli/sessions/`. The source above wins.
- **Hooks:** none found. Windows unknown.

### OpenHands CLI
- **Store (DOCS):** `~/.openhands/conversations/<conversation-id>/conversation.json` (https://docs.openhands.dev/openhands/usage/cli/resume). The SDK layout is `base_state.json` plus `events/event-NNNNN-<id>.json` (https://docs.openhands.dev/sdk/guides/convo-persistence.md). The two layouts conflict; inspect real files. No env override or Windows path stated.
- **Hooks:** none found.

### Other agents seen
- **Mistral Vibe:** logs under `~/.vibe/logs/session`, relocatable with `VIBE_HOME` (3P mirror only).
- **Augment Auggie:** `auggie session list/resume` exist; on-disk path unknown.
- **Junie, Warp, Zed agent, Codebuff:** nothing found.

## (c) Reader design implications
- **Tier 1, JSONL parseable now:** Claude Code, Codex, Gemini CLI (`.jsonl` plus legacy `.json`), Cursor agent-transcripts, Antigravity CLI `transcript.jsonl`, Kimi, pi, Copilot CLI, and Factory (once the path is settled). Each needs its own adapter; the shapes differ.
- **Tier 1 shared:** the shapes below are consistent, so one reader function can serve each group.
  - Claude-like block shape: Claude Code and Cursor (`message.content[]` with `tool_use`/`tool_result`).
  - Envelope shape `{timestamp,type,payload}`: Codex only.
  - Tree-linked JSONL: pi.
- **Tier 2, needs SQLite (open read-only; opencode runs in WAL mode):** opencode, Goose, Crush, Cline CLI, Kiro, Cursor `store.db`, Antigravity CLI `.db`. The last two carry protobuf or content-addressed blobs and are not directly queryable.
- **Tier 3, single-document JSON:** Continue, OpenHands, legacy Gemini, Amp. Whole-file parse; these are not tailable.
- **Opaque or lossy:** Antigravity `.pb` and IDE `.pb` (use the `transcript*.jsonl` and `overview.txt` side channels), Aider (Markdown only), Cursor protobuf blobs.
- **Live interception (argshape-style):** hook agents with a JSON-on-stdin and deny contract are Claude Code, Codex (Bash matcher), Gemini CLI (BeforeTool), Cursor (`beforeShellExecution`, partial CLI), Factory, Kimi, Qwen Code, Copilot CLI, Kiro, Goose, and Cline. They need per-agent shims; the field names for tool name, input, and exit status differ. In-process plugin APIs are needed for opencode (`tool.execute.before`), pi (`tool_call`), and Amp (`tool.call`). The rest are post-hoc only.
- **Compaction and subagents:**
  - Claude Code: subagents are separate files under `<session>/subagents/`.
  - Codex: subagents are separate rollouts.
  - Gemini: subagents live in a subfolder named for the parent.
  - Kimi: subagents are under `subagents/<agent_id>/`.
  - pi: compaction is an in-file entry.
  - opencode: subagents are `session` rows with `parent_id`.
- **Dedup and multiple accounts:** Codex needs cross-root dedupe by session id (already done in `token_ledger.py`). `~/.gemini/antigravity-ide` is a byte duplicate of `~/.gemini/antigravity`.
- **Path discovery hazards:** Gemini project dir was a hash in 2025 and is a short name in 2026. opencode's db name has a channel suffix, so glob it. Crush and Aider data is project-local. Goose, Kiro, and Kimi vary by version.
- **Public-repo hygiene:** tool inputs and outputs in all of these stores can contain secrets, and transcripts are plaintext. Readers should emit counts and keys, not content.

## Exhaustion note (negatives are bounded by this search)
- **Searched on disk:** Globs of `~/.claude`, `~/.codex`, `~/.gemini`, `~/.cursor`, `~/.factory`, `~/.local/share/opencode`, `~/.config/opencode`, the Orca codex account homes, and Antigravity brain dirs. Several wildcard globs were truncated at 100 results, so counts like "9 jsonl" and "159 sessions" come from targeted patterns, not full enumeration.
- **NOT done:** opening any SQLite file with a SQL client (no Bash), full key enumeration of Codex rollouts beyond `token_ledger.py`, or checking macOS or Windows machines. All macOS and Windows entries are doc or source reads, or unknown.
- **Weak spots:**
  - Hooks for Crush, Continue, OpenHands, and Aider come from one failed search each, which is not proof of absence.
  - Several WebFetch summaries were truncated at 100k characters (Amp plugin API, Goose session manager).
  - The `cleanupPeriodDays` entry was confirmed from the `claude-directory` page, not the settings reference.

## What I'd try next with more budget
1. With a shell, run `sqlite3 .tables/.schema` on `opencode-.db`, one agy `.db`, and a Cursor `store.db` to turn the 3P schema claims into DISK findings.
2. Read opencode's `db.ts` in the correct repo, and the Qwen `chatRecordingService`, to confirm the Qwen chat format.
3. Check `transcript.jsonl` result steps for exit-code text and fix the step-pairing rule, then add a `transcript.jsonl` reader to `agy_transcripts.py` (cross-boundary lane: `agy_transcripts.py:11,285`).
4. Fetch current Kimi, Cline, Goose, Kiro, and Copilot docs and source for hook stdin schemas, and for Windows paths for every agent marked unknown.
5. Install-and-run probes (or find a Windows machine) for Factory, Amp, Kiro, Copilot, Qwen, and Crush to settle the contested store paths.

Sources: [Claude directory](https://code.claude.com/docs/en/claude-directory.md), [Claude settings](https://code.claude.com/docs/en/settings.md), [Claude hooks](https://docs.anthropic.com/en/docs/claude-code/hooks), [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Codex rollout lib.rs](https://raw.githubusercontent.com/openai/codex/main/codex-rs/rollout/src/lib.rs), [Gemini hooks reference](https://geminicli.com/docs/hooks/reference/), [Gemini chatRecordingService](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/services/chatRecordingService.ts), [opencode plugins](https://opencode.ai/docs/plugins/), [Cursor hooks](https://cursor.com/docs/hooks), [Factory hooks reference](https://docs.factory.com/reference/hooks-reference.md), [Kimi data locations](https://moonshotai.github.io/kimi-cli/en/configuration/data-locations.html), [Kimi hooks](https://moonshotai.github.io/kimi-cli/en/customization/hooks.html), [pi session format](https://pi.dev/docs/latest/session-format), [pi extensions](https://pi.dev/docs/latest/extensions), [Kiro sessions](https://kiro.dev/docs/cli/chat/session-management/), [Kiro hooks](https://kiro.dev/docs/hooks/), [Amp plugin API](https://ampcode.com/docs/plugin-api), [Goose session_manager.rs](https://raw.githubusercontent.com/block/goose/main/crates/goose/src/session/session_manager.rs), [Goose paths.rs](https://raw.githubusercontent.com/block/goose/main/crates/goose/src/config/paths.rs), [Aider options](https://aider.chat/docs/config/options.html), [Qwen storage.ts](https://raw.githubusercontent.com/QwenLM/qwen-code/main/packages/core/src/config/storage.ts), [Qwen hooks](https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks), [Crush load.go](https://raw.githubusercontent.com/charmbracelet/crush/main/internal/config/load.go), [Copilot CLI hooks reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference), [Cline CLI configuration](https://docs.cline.bot/cline-cli/configuration), [Continue session.ts](https://raw.githubusercontent.com/continuedev/continue/main/extensions/cli/src/session.ts), [OpenHands resume](https://docs.openhands.dev/openhands/usage/cli/resume).