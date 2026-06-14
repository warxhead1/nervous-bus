# Reflexarc adapter template

Scaffold for a **private, project-specific** Reflexarc adapter. The generic engine
(`nervous-bus/adapters/reflex-recorder/`) stays public; project-specific taxonomy,
detectors, and cost signals live in `$NERVOUS_HOME/adapters/reflex-<project>/`.

## Create one

```bash
cp -r nervous-bus/adapters/reflex-recorder/templates/reflex-adapter \
      ~/.config/nervous-bus/adapters/reflex-myproject
cd ~/.config/nervous-bus/adapters/reflex-myproject
mv PROJECT_detectors myproject_detectors
mv PROJECT_signals   myproject_signals
# edit adapter.py: set name="myproject", wire your taxonomy/detectors/signals
python3 -m pytest tests/ -q
```

## The contract (`adapter_api`)

| method | returns | purpose |
|--------|---------|---------|
| `matches(project)` | bool | does this adapter govern the project |
| `taxonomy()` | `CommandTaxonomy` | classify shell commands (build/run-verify/explore/wait/act) for the trajectory profiler |
| `detectors()` | `[BaseDetector]` | project-specific Tier-1 detectors |
| `signals()` | `[SignalIngester]` | pull non-activity cost signals (build reports, GPU diagnostics) into the store before detection |
| `replays()` | `{name: fn}` | eval replays keyed by `DETECTOR_NAME` |

## Rules

- **Engine on the path.** Detectors do `from detectors.base import BaseDetector`;
  the engine adds itself to `sys.path` during discovery. Tests set
  `REFLEX_ENGINE_HOME` or default to `~/projects/nervous-bus/adapters/reflex-recorder`.
- **Unique package names.** Name your detector/signal packages `<project>_detectors`
  / `<project>_signals` — NOT `detectors`/`signals` — to avoid shadowing the engine.
- **Never import the engine at module load if it imports you back.** Put any
  `from synthesis import ...` inside replay function bodies (lazy) to avoid cycles.
- **Real working reference:** `reflex-tengine` in the overlay.
```
reflex-<project>/
  adapter.py                       # ProjectAdapter subclass
  <project>_taxonomy.py            # optional
  <project>_detectors/__init__.py
  <project>_signals/__init__.py
  workflows/                       # optional .js workflows
  tests/
  adapter.toml
```
