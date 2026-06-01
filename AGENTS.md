# nervous-bus — instructions for autonomous agents (hearth-loom worker, deer-flow)

This repo is part of the nervous-bus ecosystem. It is designed to be worked on
autonomously by hearth-loom: pick up a bead → open PR → CI → merge.

## Beads workflow (mandatory)

```bash
bd ready                          # find ready work
bd show <id>                      # read full issue (acceptance criteria, file scope)
bd update <id> --claim            # claim before working
# ... do the work ...
git add <files> && git commit -m "..."
bd close <id> --reason="<short>"
git push
```

**Do NOT use TodoWrite or markdown task lists.** Beads is the source of truth.

## Acceptance criteria pattern (every bead must follow)

Every bead in this repo has machine-readable acceptance criteria. Example:

```yaml
acceptance:
  - file_exists: plugin/src/lib.rs
  - command_succeeds: cd plugin && cargo build --target wasm32-wasi --release
  - file_contains:
      path: plugin/src/lib.rs
      pattern: "fn pipe(.*PipeMessage)"
  - test_passes: plugin/tests/route_smoke.rs
```

If the bead lacks machine-readable acceptance, do **not** start work — file a
follow-up bead asking the proposer (deer-flow) to enrich it via the
`bead-enrichment` skill.

## File scope discipline

Every bead names the files it's allowed to modify. **Stay inside that scope.**
If you need to touch a file outside scope:

1. Stop.
2. File a new bead linking to the current one with `bd dep add`.
3. Close the current bead as `--reason="needs scope expansion, see <new-id>"`.

## Cross-repo coordination

When work spans nervous-bus + another repo (e.g. adding the SSE adapter to
deer-flow gateway), file TWO beads:

1. `nervous-bus/<n>` — the contract / schema / adapter side
2. `deer-flow/<m>` — the gateway integration

Add `bd dep add deer-flow/<m> nervous-bus/<n>` so the gateway bead blocks until
the contract is merged.

## Publishing schema changes

ANY change under `schemas/` requires:
1. Bumping the file's major version (`tengine.session.frame.v1.json` → `v2.json`)
2. Keeping the v1 file in place (deprecated, not deleted) until all known
   consumers are updated
3. Filing a `bd remember` note about the migration window

## Communication

When uncertain:
- Use `bd human <id>` to flag for human review
- Use `bd notes <id> "..."` to add context for the next agent
- NEVER silently change behavior to make tests pass

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
