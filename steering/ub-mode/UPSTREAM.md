# Upstream

| piece | source | pinned | license |
|---|---|---|---|
| skill method | [unclebob/ub-mode](https://github.com/unclebob/ub-mode) `SKILL.md` | adapted; some sentences kept | MIT, (c) 2026 Robert C. Martin, notice in `LICENSE.ub-mode` |
| crapper | [unclebob/crapper](https://github.com/unclebob/crapper) | `c757ec79a6c15dc815238bbd9c2f4601eb54d3cc` | 0BSD |
| dryer | [unclebob/dryer](https://github.com/unclebob/dryer) | `66ff6d21a42c04afcad89c78a80066176d1294b0` | 0BSD |
| mutator | [unclebob/mutator](https://github.com/unclebob/mutator) | `29afb427129b6bf03699286966fc55ec8a4830d7` | 0BSD |
| tree-sitter | PyPI | `0.26.0`, hash-locked | MIT |
| setuptools | PyPI | `84.2.1`, hash-locked; builds the three tools with no build isolation | MIT |
| tree-sitter-language-pack | PyPI | `1.24.0`, hash-locked; parser bundle sha256 in `lock/pins.json` | MIT |

`uml-viewer` (Clojure) reads the same `.metrics/` files and is not installed here.

## What the tools do on their own that ub-gate stops

- tree-sitter-language-pack 1.x downloads a ~23 MB bundle of compiled parsers
  from its GitHub release on first use and loads it. Its sha256 is checked
  against a manifest fetched from the same release. ub-gate downloads it once at
  install and refuses to run if the bundle or manifest differs from the sha256
  pinned in this repo.
- crapper's default coverage run installs packages without asking: `pip install
  coverage pytest` into the project's interpreter, `npm install
  @vitest/coverage-v8`, `npx c8`, and `cargo install cargo-llvm-cov`. Under
  ub-gate these fail closed (no network, offline settings), and the skill
  says to supply coverage instead.
- None of the three tools opens a socket itself.

## Changes in this plugin

- **Dependency cooldown.** `lock/requirements.lock` was compiled with
  `uv pip compile --exclude-newer` 14 days back, so a release from the last two
  weeks never lands. OSV listed no known vulnerabilities for any of the three
  pins on 2026-11-20. pip builds each tool from its pinned commit with the
  index switched off, so a build cannot fetch an unpinned setuptools. To move
  a pin: recompile with a new cutoff, check OSV, run the three tools' own test
  suites offline, and update `lock/pins.json`.
- **Exact similarity join for dryer** (`bin/simjoin.py`). dryer scores every
  pair of forms in a language. On a 1.7M-line Go repo it was stopped at a 30-minute cap unfinished. The
  prefix-filter join took 50 s there, and its output was byte-identical to
  stock dryer on every tree where both finished. `--against-repo` and `--since` compare changed code against the whole
  project, and generated files are skipped.
- **The skill.** The method keeps upstream's "make it work, then make it right"
  and the three tool gates, with what measuring them taught. Gate the changed
  code, not the repo. Supply coverage that matches what you score. Group
  duplicate pairs before counting. Name equivalent mutants rather than
  reshaping code to kill them. Upstream's Practice section (log every action,
  push to the main line, worktree cleanup) is left out: those are each
  workplace's own rules.
