# ub-mode

A skill (`skills/ub-mode/SKILL.md`) and a CLI (`bin/ub-gate`) that runs
unclebob's crapper, dryer, and mutator at pinned versions with no network.
In Claude Code, install the plugin from the nervous-bus marketplace. The steps
below are for any other agent that reads `SKILL.md` skills (Kiro CLI, Codex)
or for running `ub-gate` by hand.

## Get it

Clone only this folder (git also checks out the top-level files):

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/warxhead1/nervous-bus ~/.local/share/ub-mode-src
git -C ~/.local/share/ub-mode-src sparse-checkout set steering/ub-mode
```

Put `ub-gate` on PATH. It finds `lock/` next to its own `bin/` through the
link, so keep the folder in one piece:

```bash
ln -s ~/.local/share/ub-mode-src/steering/ub-mode/bin/ub-gate ~/.local/bin/ub-gate
```

Link the skill where your agent looks for skills. For Kiro CLI:

```bash
ln -s ~/.local/share/ub-mode-src/steering/ub-mode/skills/ub-mode ~/.kiro/skills/ub-mode
```

## Install the tools

```bash
ub-gate install
```

This is the only step that uses the network. It needs Python 3.11 or newer
and `git`. It fetches crapper, dryer, and mutator from GitHub at the commits in
`lock/pins.json`. It installs their Python packages from the hash-locked
`lock/requirements.lock` and checks the parser bundle against its pinned
sha256. Nothing is vendored in this repo. A host that cannot reach
github.com and pypi.org cannot run this step.

Every other command runs in a network namespace with only loopback. macOS has
none, so set `UB_GATE_NETWORK=allow` there.

To update, run `git -C ~/.local/share/ub-mode-src pull`, then `ub-gate
install`. A changed pin installs into a new venv.

Provenance and licenses: `UPSTREAM.md`.
