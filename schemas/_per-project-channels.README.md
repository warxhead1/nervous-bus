# Per-project channel schemas

These schemas (`_per-project.*.v1.json`) are SHARED BASE schemas for the
hearth-loom per-project customization system. They are not direct event types
— the leading underscore signals "template".

Concrete event types use `<project>.<channel>.<event>.v1`, e.g.:
- `home-automation.capabilities.advertised.v1`
- `tengine.research.finding.v1`
- `deer-flow.pattern.discovered.v1`

Per-project schema files (one per project per channel) `$ref` these bases:

```json
{ "$ref": "_per-project.capabilities.advertised.v1.json" }
```

Per-project files are added in Phase 1 of the per-project customization
rollout (starting with home-automation).

## Channel summary

| Channel | Emitter | Purpose |
|---|---|---|
| `capabilities.advertised` | Project bootstrap / hearth-loom file watcher | Announces the project's bundle |
| `skill.push` | hearth-loom on proposal-bead close | Hot-add a skill to running profiles |
| `rule.push` | hearth-loom on proposal-bead close | Append rule to AGENTS.md, hot-inject via inbox |
| `research.finding` | deer-flow / researchers | Cross-project findings → advisory beads |
| `pattern.discovered` | Tier 4 / deer-improve | Recurring pattern → proposal bead |
