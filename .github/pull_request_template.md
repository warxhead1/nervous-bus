## What

<!-- One sentence: what does this PR change? -->

## Why

<!-- Why is this change needed? Link bead ID if applicable (nervous-bus-xxxx) -->

## Schema impact

<!-- Does this PR add, change, or deprecate a schema? -->
- [ ] No schema changes
- [ ] New schema added (schema file created BEFORE any publisher code)
- [ ] Schema version bumped (breaking change → new v<n>.json, old file kept with `"deprecated": true`)
- [ ] Schema deprecated

## Checklist

- [ ] `cargo test` passes (`cd plugin && cargo test`)
- [ ] Schema lint passes (all schemas are valid JSON with `$id`, `title`, `description`)
- [ ] Shell scripts pass shellcheck
- [ ] No direct `zellij pipe` calls — all publishes go through `nervous publish`
- [ ] No business logic added to the plugin (routing only)
- [ ] Commit messages follow Conventional Commits (`feat:`, `fix:`, `chore:`, etc.)
