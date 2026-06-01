# vendored fork of atty

Source: https://github.com/softprops/atty @ 0.2.14 (last upstream release)

## Why this is vendored

`atty` is unmaintained — the maintainer has been unreachable for years. RUSTSEC-2021-0145 / GHSA-g98v-hv3f-hcfr (potential unaligned read on Windows) was reported in 2021 and has a pending PR (#51) that has never been merged.

Our plugin compiles to `wasm32-wasi`, where the Windows code path is unreachable, so the advisory is structurally inapplicable to our build. We vendor + patch anyway so:

1. Dependabot stops alerting on every push (alerts as `low` were getting tuned out).
2. Anyone reading `Cargo.lock` sees an audit trail rather than a known-vulnerable version.
3. We are not on the hook to migrate the entire upstream zellij dependency chain to clap 4.

## What changed vs upstream 0.2.14

Applied [softprops/atty PR #51](https://github.com/softprops/atty/pull/51) ("fix dereferencing of unaligned FILE_NAME_INFO") verbatim — the fix swaps the heap-allocated `Vec<u8>` cast to `*const FILE_NAME_INFO` for a stack-allocated `#[repr(C)]` struct with a fixed-size `FileName` array, eliminating the alignment hazard. Also bumped `version` 0.2.14 → 0.2.15 so dependabot's vulnerable-range check (`<= 0.2.14`) no longer matches.

## How it is wired up

`plugin/Cargo.toml` contains:

```toml
[patch.crates-io]
atty = { path = "../vendor/atty" }
```

Cargo's patch table replaces the registry version everywhere it appears in the dependency graph (transitively via `clap 3 → zellij-utils → zellij-tile`).

## When to delete this vendor

Delete `vendor/atty/` and the `[patch.crates-io]` entry once the upstream `zellij` chain migrates off clap 3 — at that point atty drops out of the graph entirely and there is nothing left to patch.
