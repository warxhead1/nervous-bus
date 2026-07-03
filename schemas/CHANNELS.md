# Channel taxonomy

Generated index of every channel schema in `schemas/*.json`, clustered by domain. **Do not hand-edit** — regenerate with `python3 tools/gen_channels_md.py` after adding or renaming a schema.

Discover from the CLI: `nervous schemas --cluster <name>` filters to one cluster, `nervous schemas --search <keyword>` does a substring match.

**291 channels** across 5 clusters.

| Cluster | Channels | Scope |
| --- | --: | --- |
| [Session Lifecycle](#session-lifecycle) | 19 | agent session lifecycle, heartbeats, thread/run start-stop |
| [Autobench](#autobench) | 53 | autobench.* evolution loop (case/judge/improver/budget/...) |
| [Hearth](#hearth) | 49 | hearth-loom PR pipeline, bead lifecycle, loom executions |
| [Tengine](#tengine) | 42 | tengine shadergen + silo session telemetry |
| [Cross-cutting](#cross-cutting) | 128 | bus internals, kb, GPU kernels, funsearch, system/pulse, per-project broadcast |

## Session Lifecycle

_agent session lifecycle, heartbeats, thread/run start-stop_

| Channel | Description |
| --- | --- |
| `agent.message.v1` | agent.message v1 |
| `agent.session.heartbeat.v1` | agent.session.heartbeat v1 |
| `agent.session.linked.v1` | agent.session.linked v1 |
| `agent.session.snapshot.v1` | agent.session.snapshot v1 |
| `agent.session.v1` | agent.session v1 |
| `bus.agent.activity.v1` | bus.agent.activity v1 |
| `bus.agent.heartbeat.v1` | bus.agent.heartbeat v1 |
| `bus.agent.run.closed.v1` | bus.agent.run.closed v1 |
| `bus.agent.run.eval.v1` | bus.agent.run.eval v1 |
| `bus.agent.watchdog.v1` | bus.agent.watchdog v1 |
| `deer-flow.council.session.v1` | deer-flow.council.session v1 |
| `deer-flow.forge.session.council_linked.v1` | deer-flow.forge.session.council_linked v1 |
| `deer-flow.forge.session.created.v1` | deer-flow.forge.session.created v1 |
| `deer-flow.forge.session.hypothesis_revised.v1` | deer-flow.forge.session.hypothesis_revised v1 |
| `deer-flow.forge.session.transitioned.v1` | deer-flow.forge.session.transitioned v1 |
| `hearth.session.completed.v1` | hearth.session.completed v1 |
| `kb.session.context.v1` | KB Session Context |
| `kb.session.harvest.v1` | kb.session.harvest v1 |
| `kb.session.indexed.v1` | 🔇 **unconsumed** — KB Session Indexed |

## Autobench

_autobench.* evolution loop (case/judge/improver/budget/...)_

| Channel | Description |
| --- | --- |
| `autobench.adversarial.curveball_generated.v1` | autobench.adversarial.curveball_generated v1 |
| `autobench.adversarial.round_complete.v1` | autobench.adversarial.round_complete v1 |
| `autobench.budget.gauge.v1` | autobench.budget.gauge v1 |
| `autobench.budget.rate.v1` | autobench.budget.rate v1 |
| `autobench.budget.warning.v1` | autobench.budget.warning v1 |
| `autobench.case.result.v1` | autobench.case.result v1 |
| `autobench.command.acknowledged.v1` | autobench.command.acknowledged v1 |
| `autobench.command.v1` | autobench.command v1 |
| `autobench.continuous.digest.v1` | autobench.continuous.digest v1 |
| `autobench.continuous.promotion_decision.v1` | autobench.continuous.promotion_decision v1 |
| `autobench.continuous.session_complete.v1` | autobench.continuous.session_complete v1 |
| `autobench.cross_domain.evaluation.v1` | autobench.cross_domain.evaluation v1 |
| `autobench.curriculum.cycle.v1` | autobench.curriculum.cycle v1 |
| `autobench.curriculum.problem.rejected.v1` | autobench.curriculum.problem.rejected v1 |
| `autobench.curriculum.problem.v1` | autobench.curriculum.problem v1 |
| `autobench.cycle.report.v1` | autobench.cycle.report v1 |
| `autobench.cycle.requested.v1` | autobench.cycle.requested v1 |
| `autobench.diversity.v1` | autobench.diversity v1 |
| `autobench.failure.category.v1` | autobench.failure.category.v1 |
| `autobench.failure_pattern.v1` | autobench.failure_pattern v1 |
| `autobench.gpu_job.v1` | autobench.gpu_job.v1 |
| `autobench.gpu_result.v1` | autobench.gpu_result.v1 |
| `autobench.heartbeat.timeout.v1` | autobench.heartbeat.timeout v1 |
| `autobench.improver.convergence.threshold_adapted.v1` | autobench.improver.convergence.threshold_adapted v1 |
| `autobench.improver.delta.diff.v1` | autobench.improver.delta.diff v1 |
| `autobench.improver.divergence.v1` | autobench.improver.divergence v1 |
| `autobench.improver.ensemble.v1` | autobench.improver.ensemble v1 |
| `autobench.improver.prediction.clipped.v1` | autobench.improver.prediction.clipped v1 |
| `autobench.improver.prediction.refuted_live.v1` | autobench.improver.prediction.refuted_live v1 |
| `autobench.improver.prediction.v1` | autobench.improver.prediction v1 |
| `autobench.improver.prediction.verified.v1` | autobench.improver.prediction.verified v1 |
| `autobench.improver.reasoning.v1` | autobench.improver.reasoning v1 |
| `autobench.improver.v1` | autobench.improver v1 |
| `autobench.invalidation.v1` | autobench.invalidation v1 |
| `autobench.island.health.v1` | autobench.island.health v1 |
| `autobench.iteration.summary.v1` | autobench.iteration.summary v1 |
| `autobench.iteration.v1` | autobench.iteration v1 |
| `autobench.judge.disagreement.v1` | autobench.judge.disagreement v1 |
| `autobench.judge.dissent_weight.v1` | autobench.judge.dissent_weight v1 |
| `autobench.judge.pool.verdict.v1` | autobench.judge.pool.verdict v1 |
| `autobench.phase.v1` | autobench.phase v1 |
| `autobench.population.summary.v1` | autobench.population.summary v1 |
| `autobench.refactor.v1` | autobench.refactor v1 |
| `autobench.result.v1` | autobench.result v1 |
| `autobench.rsi.checkpoint_revert.v1` | autobench.rsi.checkpoint_revert v1 |
| `autobench.rsi.completed.v1` | autobench.rsi.completed v1 |
| `autobench.sandbox.stderr.v1` | autobench.sandbox.stderr v1 |
| `autobench.sandbox.v1` | autobench.sandbox v1 |
| `autobench.scoring.weights_adapted.v1` | autobench.scoring.weights_adapted v1 |
| `autobench.shader.v1` | autobench.shader v1 |
| `autobench.symbol.lineage.v1` | autobench.symbol.lineage v1 |
| `autobench.worker.queue_pressure.v1` | autobench.worker.queue_pressure v1 |
| `autobench.worker.v1` | autobench.worker v1 |

## Hearth

_hearth-loom PR pipeline, bead lifecycle, loom executions_

| Channel | Description |
| --- | --- |
| `bus.bead.bench_completed.v1` | bus.bead.bench_completed v1 |
| `bus.bead.closed.v1` | bus.bead.closed v1 |
| `bus.bead.created.v1` | bus.bead.created v1 |
| `bus.bead.lifecycle.v1` | bus.bead.lifecycle v1 |
| `bus.bead.pr_opened.v1` | bus.bead.pr_opened v1 |
| `bus.bead.scored.v1` | bus.bead.scored v1 |
| `bus.bead.status_changed.v1` | bus.bead.status_changed v1 |
| `bus.bead.updated.v1` | bus.bead.updated v1 |
| `bus.beads.command.v1` | Beads inbound command |
| `bus.beads.issue.v1` | Beads issue mutation event |
| `bus.beads.kanban.snapshot.v1` | Beads kanban board snapshot |
| `bus.hearth.approval.responded.v1` | bus.hearth.approval.responded v1 |
| `bus.hearth.bead.changes.v1` | bus.hearth.bead.changes v1 |
| `bus.hearth.dispatch.requested.v1` | bus.hearth.dispatch.requested v1 |
| `bus.hearth.session.activity.v1` | bus.hearth.session.activity v1 |
| `bus.hearth.session.ended.v1` | bus.hearth.session.ended v1 |
| `bus.hearth.session.idle.v1` | bus.hearth.session.idle v1 |
| `bus.hearth.session.permission.requested.v1` | bus.hearth.session.permission.requested v1 |
| `bus.hearth.session.permission.responded.v1` | bus.hearth.session.permission.responded v1 |
| `bus.hearth.session.started.v1` | bus.hearth.session.started v1 |
| `bus.hearth.subagent.lifecycle.v1` | bus.hearth.subagent.lifecycle v1 |
| `bus.hearth.tengine.silo.requested.v1` | bus.hearth.tengine.silo.requested v1 |
| `bus.hearth.variant.voted.v1` | bus.hearth.variant.voted v1 |
| `bus.hearth.wave.gate.responded.v1` | bus.hearth.wave.gate.responded v1 |
| `hearth-loom.ac.verified.v1` | hearth-loom.ac.verified v1 |
| `hearth-loom.bench.completed.v1` | hearth-loom.bench.completed v1 |
| `hearth.command.design_request.v1` | hearth.command.design_request v1 |
| `hearth.design.generated.v1` | hearth.design.generated v1 |
| `hearth.drift.detected.v1` | hearth.drift.detected v1 |
| `hearth.ember.insight.v1` | hearth.ember.insight v1 |
| `hearth.ember.learning.insight.v1` | hearth.ember.learning.insight v1 |
| `hearth.inference.error.v1` | hearth.inference.error v1 |
| `hearth.integration.error.v1` | hearth.integration.error v1 |
| `hearth.kb.article.saved.v1` | hearth.kb.article.saved v1 |
| `hearth.perf.ai_call.v1` | hearth.perf.ai_call v1 |
| `hearth.perf.slow_query.v1` | hearth.perf.slow_query v1 |
| `hearth.router.decision.v1` | hearth.router.decision v1 |
| `hearth.voice.intent.v1` | hearth.voice.intent v1 |
| `hearth.voice.pipeline.v1` | hearth.voice.pipeline v1 |
| `loom.coord.v1` | loom.coord v1 |
| `loom.lifecycle.ci.v1` | loom.lifecycle.ci v1 |
| `loom.lifecycle.phase.v1` | loom.lifecycle.phase v1 |
| `loom.lifecycle.pr.v1` | loom.lifecycle.pr v1 |
| `loom.lifecycle.retry.v1` | loom.lifecycle.retry v1 |
| `loom.lifecycle.v1` | loom.lifecycle v1 |
| `loom.plan.step.v1` | loom.plan.step v1 |
| `loom.plan.v1` | loom.plan v1 |
| `loom.runner.v1` | loom.runner v1 |
| `loom.shared-session.v1` | loom.shared-session v1 |

## Tengine

_tengine shadergen + silo session telemetry_

| Channel | Description |
| --- | --- |
| `tengine.checkpoint.captured.v1` | tengine.checkpoint.captured v1 |
| `tengine.checkpoint.restored.v1` | tengine.checkpoint.restored v1 |
| `tengine.checkpoint.validated.v1` | tengine.checkpoint.validated v1 |
| `tengine.checkpoint.warmup_chain_timing.v1` | tengine.checkpoint.warmup_chain_timing v1 |
| `tengine.code.changed.v1` | tengine.code.changed v1 |
| `tengine.contract.state.v1` | tengine.contract.state v1 |
| `tengine.contract.violation.v1` | tengine.contract.violation v1 |
| `tengine.frame.metrics.v1` | 🔇 **orphaned-consumer-mismatch** — tengine.frame.metrics v1 |
| `tengine.gpu.lease.granted.v1` | tengine.gpu.lease.granted v1 |
| `tengine.gpu.lease.heartbeat.v1` | tengine.gpu.lease.heartbeat v1 |
| `tengine.gpu.lease.released.v1` | tengine.gpu.lease.released v1 |
| `tengine.race.brain.v1` | tengine.race.brain v1 |
| `tengine.race.episode.v1` | tengine.race.episode v1 |
| `tengine.race.event.v1` | tengine.race.event v1 |
| `tengine.session.biome_palette_loaded.v1` | tengine.session.biome_palette_loaded v1 |
| `tengine.session.client_connected.v1` | tengine.session.client_connected v1 |
| `tengine.session.client_disconnected.v1` | tengine.session.client_disconnected v1 |
| `tengine.session.client_identified.v1` | tengine.session.client_identified v1 |
| `tengine.session.fps_drop.v1` | tengine.session.fps_drop v1 |
| `tengine.session.frame.v1` | tengine.session.frame v1 |
| `tengine.session.frame_milestone.v1` | tengine.session.frame_milestone v1 |
| `tengine.session.snapshot_ready.v1` | tengine.session.snapshot_ready v1 |
| `tengine.session.start.v1` | tengine.session.start v1 |
| `tengine.session.stop.v1` | tengine.session.stop v1 |
| `tengine.session.warmup_complete.v1` | tengine.session.warmup_complete v1 |
| `tengine.shadergen.cmd.v1` | tengine.shadergen.cmd v1 |
| `tengine.shadergen.diagnose_completed.v1` | tengine.shadergen.diagnose_completed v1 |
| `tengine.shadergen.eval.completed.v1` | tengine.shadergen.eval.completed v1 |
| `tengine.shadergen.eval.requested.v1` | tengine.shadergen.eval.requested v1 |
| `tengine.shadergen.multiverse.v1` | tengine.shadergen.multiverse v1 |
| `tengine.shadergen.nbus_cmd_received.v1` | tengine.shadergen.nbus_cmd_received v1 |
| `tengine.shadergen.screenshot.v1` | tengine.shadergen.screenshot v1 |
| `tengine.shadergen.shader_reloaded.v1` | tengine.shadergen.shader_reloaded v1 |
| `tengine.shadergen.snap_completed.v1` | tengine.shadergen.snap_completed v1 |
| `tengine.shadergen.sync.v1` | tengine.shadergen.sync v1 |
| `tengine.silo.started.v1` | tengine.silo.started v1 |
| `tengine.silo.verify.v1` | tengine.silo.verify v1 |
| `tengine.stream.dump_ready.v1` | tengine.stream.dump_ready v1 |
| `tengine.telemetry.effect_summary.v1` | tengine.telemetry.effect_summary v1 |
| `tengine.telemetry.probe_record.v1` | tengine.telemetry.probe_record v1 |
| `tengine.telemetry.scheduler_gate.v1` | tengine.telemetry.scheduler_gate v1 |
| `tengine.test.v1` | tengine.test.v1 |

## Cross-cutting

_bus internals, kb, GPU kernels, funsearch, system/pulse, per-project broadcast_

| Channel | Description |
| --- | --- |
| `_per-project.capabilities.advertised.v1` ⚠️ | _per-project.capabilities.advertised.v1 |
| `_per-project.pattern.discovered.v1` ⚠️ | _per-project.pattern.discovered.v1 |
| `_per-project.research.finding.v1` ⚠️ | _per-project.research.finding.v1 |
| `_per-project.rule.push.v1` ⚠️ | _per-project.rule.push.v1 |
| `_per-project.skill.push.v1` ⚠️ | _per-project.skill.push.v1 |
| `bus.dashboard.v1` | bus.dashboard v1 |
| `bus.dead_letter.v1` | bus.dead_letter v1 |
| `bus.dispatch.recycle.completed.v1` | bus.dispatch.recycle.completed v1 |
| `bus.dispatch.recycle.skipped.v1` | bus.dispatch.recycle.skipped v1 |
| `bus.exec.lifecycle.v1` | bus.exec.lifecycle v1 |
| `bus.hearth-loom.ac.verified.v1` | bus.hearth-loom.ac.verified v1 |
| `bus.intrinsic.marker.v1` | bus.intrinsic.marker v1 |
| `bus.notify.v1` | bus.notify v1 |
| `bus.pattern.bundle.v1` | bus.pattern.bundle v1 |
| `bus.pattern.feedback.v1` | bus.pattern.feedback v1 |
| `bus.pattern.signal.v1` | bus.pattern.signal v1 |
| `bus.redis-mirror.config.v1` | bus.redis-mirror.config v1 |
| `bus.redis.consumer.lag.v1` | bus.redis.consumer.lag v1 |
| `bus.redis.stream.health.v1` | bus.redis.stream.health v1 |
| `bus.remediation.applied.v1` | bus.remediation.applied v1 |
| `bus.saga.v1` | bus.saga v1 |
| `bus.subscribers.snapshot.v1` | bus.subscribers.snapshot v1 |
| `bus.system.heartbeat.v1` | bus.system.heartbeat v1 |
| `bus.tengine.agent.lifecycle.v1` | bus.tengine.agent.lifecycle v1 |
| `bus.tengine.agent.thought.v1` | bus.tengine.agent.thought v1 |
| `bus.tengine.antidefer.flagged.v1` | bus.tengine.antidefer.flagged v1 |
| `bus.tengine.approval.requested.v1` | bus.tengine.approval.requested v1 |
| `bus.tengine.biome_layer_applied.v1` | bus.tengine.biome_layer_applied v1 |
| `bus.tengine.bridge.path_verified.v1` | bus.tengine.bridge.path_verified.v1 |
| `bus.tengine.bridge_path_verified.v1` | bus.tengine.bridge_path_verified v1 |
| `bus.tengine.composer.dispatched.v1` | bus.tengine.composer.dispatched.v1 |
| `bus.tengine.composer.first_output.v1` | bus.tengine.composer.first_output.v1 |
| `bus.tengine.council.disagreement.v1` | bus.tengine.council.disagreement v1 |
| `bus.tengine.gate.skipped.v1` | bus.tengine.gate.skipped v1 |
| `bus.tengine.gpu.heartbeat.v1` | bus.tengine.gpu.heartbeat v1 |
| `bus.tengine.main.advanced.v1` | bus.tengine.main.advanced.v1 |
| `bus.tengine.mirror.verdict.v1` | bus.tengine.mirror.verdict v1 |
| `bus.tengine.phantom_wire.detected.v1` | bus.tengine.phantom_wire.detected v1 |
| `bus.tengine.screenshot.captured.v1` | bus.tengine.screenshot.captured.v1 |
| `bus.tengine.screenshot.luminance.v1` | bus.tengine.screenshot.luminance v1 |
| `bus.tengine.silo.crash.v1` | bus.tengine.silo.crash v1 |
| `bus.tengine.silo.verification.v1` | bus.tengine.silo.verification.v1 |
| `bus.tengine.test.v1` | bus.tengine.test.v1 |
| `bus.tengine.visual.regression.v1` | bus.tengine.visual.regression v1 |
| `bus.tengine.wave.gate.pending.v1` | bus.tengine.wave.gate.pending v1 |
| `bus.tengine.worktree.leakage.v1` | bus.tengine.worktree.leakage v1 |
| `bus.triage.findings.v1` | bus.triage.findings v1 |
| `bus.workflow.agent.dispatch.v1` | Workflow agent dispatch |
| `career-ops.application.submitted.v1` | career-ops.application.submitted v1 |
| `career-ops.posting.evaluated.v1` | career-ops.posting.evaluated v1 |
| `career-ops.scanner.cycle_completed.v1` | career-ops.scanner.cycle_completed v1 |
| `career-ops.steering.answered.v1` | career-ops.steering.answered v1 |
| `codeforces_problem.v1` ⚠️ | CodeforcesProblem |
| `deer-flow.agent.message.v1` | deer-flow.agent.message v1 |
| `deer-flow.agent.thread.v1` | deer-flow.agent.thread v1 |
| `deer-flow.audit.recommendation.snapshot.v1` | deer-flow.audit.recommendation.snapshot v1 |
| `deer-flow.audit.recommendation.v1` | deer-flow.audit.recommendation v1 |
| `deer-flow.bead.enrichment.complete.v1` | deer-flow.bead.enrichment.complete v1 |
| `deer-flow.bead.filed.v1` | deer-flow.bead.filed v1 |
| `deer-flow.bead.pushback.v1` | deer-flow.bead.pushback v1 |
| `deer-flow.council.completed.v1` | deer-flow.council.completed v1 |
| `deer-flow.council.profile.applied.v1` | deer-flow.council.profile.applied v1 |
| `deer-flow.council.profile.fallback_used.v1` | deer-flow.council.profile.fallback_used v1 |
| `deer-flow.council.started.v1` | deer-flow.council.started v1 |
| `deer-flow.cumulative.exit.v1` | deer-flow.cumulative.exit v1 |
| `deer-flow.cumulative.hard.v1` | deer-flow.cumulative.hard v1 |
| `deer-flow.cycle.snapshot.v1` | deer-flow.cycle.snapshot v1 |
| `deer-flow.cycle.wait.exit.v1` | deer-flow.cycle.wait.exit v1 |
| `deer-flow.feedback.acted.v1` | deer-flow.feedback.acted v1 |
| `deer-flow.feedback.received.v1` | deer-flow.feedback.received v1 |
| `deer-flow.forge.seal.stamped.v1` | deer-flow.forge.seal.stamped v1 |
| `deer-flow.forge.subscription.error.v1` | ForgeSubscriptionError |
| `deer-flow.guidance.fact.v1` | deer-flow.guidance.fact v1 |
| `deer-flow.metaprobe.cycle.v1` | deer-flow.metaprobe.cycle v1 |
| `deer-flow.openrouter.credit_exhausted.v1` | deer-flow.openrouter.credit_exhausted v1 |
| `deer-flow.otel.span.v1` | deer-flow.otel.span v1 |
| `deer-flow.research.cycle.completed.v1` | deer-flow.research.cycle.completed v1 |
| `deer-flow.research.cycle.started.v1` | deer-flow.research.cycle.started v1 |
| `deer-flow.research.dispatch.v1` | deer-flow.research.dispatch v1 |
| `deer-flow.research.finding.v1` | deer-flow.research.finding v1 |
| `deer-flow.sandbox.result.v1` | deer-flow.sandbox.result v1 |
| `deer-flow.sandbox.risk.v1` | deer-flow.sandbox.risk v1 |
| `deer-flow.semantic_cache.hit.v1` | deer-flow.semantic_cache.hit v1 |
| `deer-flow.semantic_cache.miss.v1` | deer-flow.semantic_cache.miss v1 |
| `deer-flow.soul.modified.v1` | deer-flow.soul.modified v1 |
| `deer-flow.stack-tuner.cycle.done.v1` | deer-flow.stack-tuner.cycle.done v1 |
| `deer-flow.stack-tuner.cycle.start.v1` | deer-flow.stack-tuner.cycle.start v1 |
| `deer-flow.stack-tuner.integrity.warn.v1` | deer-flow.stack-tuner.integrity.warn v1 |
| `deer-flow.stack-tuner.stage.done.v1` | deer-flow.stack-tuner.stage.done v1 |
| `deer-flow.stack-tuner.stage.start.v1` | deer-flow.stack-tuner.stage.start v1 |
| `deer-flow.subagent.lifecycle.v1` | deer-flow.subagent.lifecycle v1 |
| `deer-flow.telemetry.dualwrite_parity.v1` | deer-flow.telemetry.dualwrite_parity v1 |
| `deer-flow.tool.call.v1` | deer-flow.tool.call v1 |
| `deer-flow.tool.usage.v1` | deer-flow.tool.usage v1 |
| `funsearch.artifact.v1` | funsearch.artifact v1 |
| `funsearch.assessment.v1` | funsearch.assessment.v1 |
| `funsearch.calibration.v1` | funsearch.calibration v1 |
| `funsearch.engine_render.completed.v1` | funsearch.engine_render.completed.v1 |
| `funsearch.engine_render.requested.v1` | funsearch.engine_render.requested.v1 |
| `funsearch.review.v1` | funsearch.review.v1 |
| `jobops.application.updated.v1` | jobops.application.updated v1 |
| `jobops.contact.added.v1` | jobops.contact.added v1 |
| `jobops.content.queued.v1` | jobops.content.queued v1 |
| `jobops.outreach.logged.v1` | jobops.outreach.logged v1 |
| `kb.artifact.linked.v1` | 🔇 **unconsumed** — KB Artifact Linked |
| `kb.decay.applied.v1` | 🔇 **unconsumed** — KB Decay Applied |
| `kb.entry.created.v1` | KB Entry Created |
| `kb.entry.vetted.v1` | 🔇 **unconsumed** — KB Entry Vetted |
| `kb.guidance.provided.v1` | 🔇 **unconsumed** — KB Guidance Provided |
| `kb.ingest.tengine.completed.v1` | kb.ingest.tengine.completed v1 |
| `kb.knowledge.gap.v1` | 🔇 **unconsumed** — KB Knowledge Gap |
| `kb.plan.researched.v1` | 🔇 **unconsumed** — KB Plan Researched |
| `kb.plan.updated.v1` | 🔇 **unconsumed** — KB Plan Updated |
| `kb.review.approved.v1` | 🔇 **unconsumed** — KB Review Approved |
| `kb.review.rejected.v1` | 🔇 **unconsumed** — KB Review Rejected |
| `kb.review.requested.v1` | KB Review Requested |
| `kb.tier.changed.v1` | 🔇 **unconsumed** — kb.tier.changed v1 |
| `kernel.best_fitness_improved.v1` | kernel.best_fitness_improved v1 |
| `kernel.candidate.evaluated.v1` | kernel.candidate.evaluated.v1 v1 |
| `kernel.completed.v1` | kernel.completed.v1 v1 |
| `kernel.generation.completed.v1` | kernel.generation.completed.v1 v1 |
| `kernel.island_reset.v1` | kernel.island_reset v1 |
| `kernel.plateau_hint.v1` | kernel.plateau_hint v1 |
| `kernel.prior.loaded.v1` | kernel.prior.loaded v1 |
| `kernel.prior.updated.v1` | kernel.prior.updated v1 |
| `kernel.started.v1` | kernel.started.v1 v1 |
| `pulse.kernel.snapshot.v1` | pulse.kernel.snapshot v1 |
| `sys.log.entry.v1` | sys.log.entry v1 |

## Retired channels (no schema file)

Producer and schema file both removed from this repo — nothing to regenerate a row from, so these are listed by hand. Present here so contributors don't mistake silent absence for 'never existed' or 'still planned'.

- `hearth.device.state.v1` 🔇 **retired** — Formally retired (2026-07 zombie-event audit). Producer was adapters/hearth-bridge (home IoT bridge); both the adapter and its schema file were deleted from this repo in commit 8dbb391 (oss-prep private-schema migration) and never restored publicly. No consumer evidenced anywhere. Not resurrecting speculative cross-project infra without an evidenced product need.
- `hearth.presence.v1` 🔇 **retired** — Formally retired (2026-07 zombie-event audit). Same producer (adapters/hearth-bridge) and same removal commit (8dbb391) as hearth.device.state.v1. No consumer evidenced anywhere. hearth.health.snapshot.v1 was removed in the same commit and never had an evidenced producer even before removal (no publish call site found) — worth knowing if this channel is ever revisited, though it isn't itself being re-registered here.

## Naming-convention violations

Convention: `<project>.<subsystem>.<event>.v<n>` (lowercase, dot-separated, trailing major version). The following filenames do not match and are flagged with ⚠️ above:

- `_per-project.capabilities.advertised.v1` — leading underscore (template/placeholder, not a real `<project>` segment)
- `_per-project.pattern.discovered.v1` — leading underscore (template/placeholder, not a real `<project>` segment)
- `_per-project.research.finding.v1` — leading underscore (template/placeholder, not a real `<project>` segment)
- `_per-project.rule.push.v1` — leading underscore (template/placeholder, not a real `<project>` segment)
- `_per-project.skill.push.v1` — leading underscore (template/placeholder, not a real `<project>` segment)
- `codeforces_problem.v1` — too few segments — needs `<project>.<subsystem>.<event>` before `.v<n>`

