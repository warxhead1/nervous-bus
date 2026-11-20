---
name: ub-mode
description: >
  Measures whether code is right with deterministic gates instead of opinion:
  CRAP score (complexity weighted by missing coverage), structural duplication,
  and mutation testing, through `ub-gate` (unclebob's crapper, dryer, and
  mutator, pinned and run with no network). Use when writing, changing,
  refactoring, or reviewing code in Go, Python, Rust, TypeScript, Java, or
  Clojure and you need to know whether a function is too complex for its tests,
  whether new code copies code that already exists, or whether the tests would
  notice a broken line. Also use when the user runs /ub-mode or asks for CRAP,
  duplication, mutation testing, or a quality gate. Don't use for docs or
  prose, or to start a repo-wide cleanup nobody asked for.
---

# ub-mode

Make it work, then make it right. The tools tell you which code is not right yet.

## Set up once per machine

`ub-gate` is on PATH when this plugin is installed. Otherwise run
`${CLAUDE_SKILL_DIR}/../../bin/ub-gate`.

```bash
ub-gate install    # the only step that uses the network
ub-gate doctor     # JSON: installed, pin, platform, sandbox
```

`install` fetches the three tools at pinned commits and their Python packages
from a hash-locked file. It then downloads the parser grammars once and checks
them against a sha256 pinned in this plugin. Every other command runs in a
network namespace with only loopback up, so no analysis can reach the network.
On a host without unprivileged user namespaces (macOS, some locked-down Linux)
the command stops. `UB_GATE_NETWORK=allow` runs it with offline settings for
pip, npm, cargo, and Go, but no namespace.

The tools write `.metrics/` and `target/` in the project root. Add both to
`.gitignore` before the first run.

## Make it work

- Write the failing test first, and see it fail for the reason you expect.
- The test calls the code the way its user does and checks what that user sees.
  If it would still pass when every function returned nothing, throw it out.
- Get the data shape right before the logic. Check outside data where it enters,
  then trust it inside. Make illegal states impossible to write.
- Fix the cause. A guard that only hides the crash is not a fix.
- Take complexity out before you add any.

## Make it right: the three gates

Gate the code you changed, not the whole repo. In a 30,000-function Go repo,
16% of functions were over complexity 10. A repo-wide threshold fails on day
one and teaches everyone to ignore it.

### CRAP: complexity the tests do not cover

`CRAP = CC² × (1 − coverage)³ + CC`. A score under 10 is the target.

```bash
ub-gate crap --changed --no-coverage     # complexity only, fast
ub-gate crap-go internal/billing         # Go: cover DIR/... then score DIR
ub-gate crap --use-existing-coverage src # any language, your own coverage report
```

- crapper scores a function with no coverage data as 0% covered, and does not
  warn. Score only the code your coverage run covered. `crap-go` covers
  `DIR/...` and scores the same `DIR`, so the two always match.
- Do not let crapper generate coverage for a large repo. Its default runs
  `go test ./...` and installs coverage tools into the project. Use `crap-go` or
  pass `--use-existing-coverage`.
- For each changed function over 10: split it, test it, or both. A function
  that was already over 10 must not get worse.
- Complexity alone is what gocyclo already reports (crapper agrees with it
  within 2). The value is the coverage weighting: it separates the complex
  function that is well tested from the one that is not.

### Duplication: new code that copies old code

```bash
ub-gate dry --since origin/main --against-repo   # changed forms vs the whole repo
ub-gate dry src/billing                          # every pair inside a tree
```

- `--against-repo` is the check that matters. It asks whether the change added
  a near-copy of something that already exists. Plain `--changed` only compares
  the changed files with each other.
- Files marked generated (`Code generated ... DO NOT EDIT.`, `@generated`) are
  skipped. In one Go repo they were 69% of all pairs.
- The report lists pairs. One helper copied 7 times is 21 pairs. Group them
  before you count.
- A score of 1.0 on a 4 to 8 line helper usually means a shared helper or a
  standard library call already exists (`slices.Contains`, an `envOr`). Use it.
  Two forms with the same shape and different meaning are fine. Leave them.

### Mutation: would the tests notice a broken line?

```bash
ub-gate mutate path/to/file.go          # one file at a time
ub-gate mutate --scan path/to/file.go   # list the sites, run nothing
```

- A survivor is a line you can break and the tests still pass. Add the
  assertion that catches it, usually at a boundary (`>` to `>=`, `0` to `1`).
- Some mutants cannot be killed: the mutated code means the same thing. For
  example, in a clamp, `x < lo` and `x <= lo` both return `lo` at `x == lo`.
  Name these in the commit or PR, then move on. Do not reshape working code
  to kill an equivalent mutant.
- Mutator only runs mutants on covered lines. An uncovered line is a missing
  test, not a pass.
- Cost: an 85-site Go file with fast tests took 19 s on 6 workers and grew
  the Go build cache by about 200 MB. Run it on the file you changed.

`ub-gate summary` prints a JSON digest of `.metrics/` for a report or a gate.

## Refactor safely

Pin what the code does with a test, change the structure, then prove the
behavior did not change. Run the three gates again on what you touched.
