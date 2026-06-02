"""Tests for the FunSearch KernelArena (nervous-bus-tvfw).

Covers the run-scoped kernel-event ingestion in PulseState and the three
arena widgets (KernelLeaderboard / IslandHeatmap / CuriositySpikeFeed):

  * classify_kernel_event maps dynamic ``<kernel>.*`` channels and rejects
    RSI ``autobench.*`` channels
  * started/generation/best/reset/hint/completed/island_health/budget_gauge
    mutate KernelRun correctly (incl. the island-0 falsy-zero guard)
  * curiosity spikes star jumps above threshold
  * leaderboard / focused-run / feed accessors behave
"""

from __future__ import annotations

from pulse_app.state import (
    KERNEL_RUN_RECENCY_WINDOW_S,
    KERNEL_SPIKE_THRESHOLD,
    PulseState,
    classify_kernel_event,
)


def ev(t: str, d: dict, time: str = "2026-06-02T00:00:00Z") -> dict:
    return {"specversion": "1.0", "id": "x", "source": "/autobench",
            "type": t, "datacontenttype": "application/json",
            "time": time, "data": d}


RID = "01KERNELRUNTEST"


# --------------------------------------------------------------- classifier --
def test_classify_kernel_events():
    assert classify_kernel_event("tsp.generation.completed.v1") == ("tsp", "generation")
    assert classify_kernel_event("sdf.best_fitness_improved.v1") == ("sdf", "best")
    assert classify_kernel_event("terrain.kernel.started.v1") == ("terrain", "started")
    assert classify_kernel_event("sph.kernel.completed.v1") == ("sph", "completed")
    assert classify_kernel_event("phase.island_reset.v1") == ("phase", "reset")
    assert classify_kernel_event("noise.plateau_hint.v1") == ("noise", "hint")
    assert classify_kernel_event("autobench.island.health.v1") == ("?", "island_health")
    assert classify_kernel_event("autobench.budget.gauge.v1") == ("?", "budget_gauge")


def test_classify_rejects_rsi_and_junk():
    for t in (
        "autobench.improver.prediction.v1",
        "autobench.iteration.summary.v1",
        "autobench.worker.v1",
        "autobench.sandbox.stderr.v1",
        "tsp.something.unmapped.v1",
        "not-an-event",
        "",
        None,
    ):
        assert classify_kernel_event(t) is None


# ------------------------------------------------------------- ingestion -----
def test_started_then_generation_builds_history():
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {
        "run_id": RID, "instances": ["berlin52"], "n_islands": 4,
        "population_per_island": 6, "generations": 8}))
    for g, bf in [(0, 0.52), (1, 0.55), (2, 0.61)]:
        s.apply(ev("tsp.generation.completed.v1", {
            "run_id": RID, "generation": g, "best_fitness": bf,
            "mean_pop_fitness": bf - 0.1, "best_island": 0, "llm_requests": g * 3}))
    run = s.kernel_runs[RID]
    assert run.kernel == "tsp"
    assert run.instances == ["berlin52"]
    assert run.target_generations == 8
    assert run.generation == 2
    assert run.best_fitness == 0.61
    assert run.fitness_values == [0.52, 0.55, 0.61]
    assert run.is_running


def test_island_health_zero_index_not_dropped():
    """island=0 must survive — guards the ``0 or -1`` falsy-zero bug."""
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": RID, "n_islands": 2}))
    s.apply(ev("autobench.island.health.v1", {
        "run_id": RID, "generation": 1, "island": 0, "best_fitness": 0.42,
        "plateau_count": 3, "population_size": 6, "age_since_last_reset": 1}))
    run = s.kernel_runs[RID]
    assert 0 in run.island_health
    assert run.island_health[0]["plateau_count"] == 3
    assert run.island_health[0]["best_fitness"] == 0.42
    # history tuple is (gen, plateau_count, age, best_fitness)
    assert run.island_history[0] == [(1, 3, 1, 0.42)]


def test_best_fitness_jump_starred_above_threshold():
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": RID}))
    big = KERNEL_SPIKE_THRESHOLD + 0.1
    small = KERNEL_SPIKE_THRESHOLD / 2
    s.apply(ev("tsp.best_fitness_improved.v1", {
        "run_id": RID, "generation": 1, "best_fitness": 0.7,
        "improvement_delta": big, "best_island": 1}))
    s.apply(ev("tsp.best_fitness_improved.v1", {
        "run_id": RID, "generation": 2, "best_fitness": 0.71,
        "improvement_delta": small, "best_island": 1}))
    jumps = [sp for sp in s.curiosity_feed() if sp.kind == "jump"]
    starred = {round(sp.magnitude, 4): sp.starred for sp in jumps}
    assert starred[round(big, 4)] is True
    assert starred[round(small, 4)] is False
    assert s.kernel_runs[RID].best_island == 1  # island 1, not dropped to -1


def test_reset_and_completed_update_run():
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": RID}))
    s.apply(ev("tsp.island_reset.v1", {
        "run_id": RID, "generation": 4, "n_islands_culled": 2,
        "pre_reset_best_fitness": 0.61, "plateau_count": 4}))
    s.apply(ev("tsp.kernel.completed.v1", {
        "run_id": RID, "total_generations": 8, "stop_reason": "generation cap",
        "llm_requests": 24,
        "best_program": {"fitness": 0.716, "priority_code": "double f(){return 1;}"}}))
    run = s.kernel_runs[RID]
    assert run.status == "complete"
    assert run.n_resets == 1
    assert 4 in run.reset_gens
    assert run.best_fitness == 0.716
    assert run.final_code.startswith("double f")
    assert run.stop_reason == "generation cap"
    kinds = {sp.kind for sp in s.curiosity_feed()}
    assert {"reset", "complete"} <= kinds


def test_budget_gauge_tracks_requests():
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": RID}))
    s.apply(ev("autobench.budget.gauge.v1", {
        "run_id": RID, "generation": 5, "requests_used": 17, "max_requests": 40}))
    run = s.kernel_runs[RID]
    assert run.requests_used == 17
    assert run.max_requests == 40


# --------------------------------------------------------------- accessors ---
def test_leaderboard_orders_running_first():
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": "RUN_A"}))
    s.apply(ev("sdf.kernel.started.v1", {"run_id": "RUN_B"}))
    s.apply(ev("sdf.kernel.completed.v1", {
        "run_id": "RUN_B", "total_generations": 3, "stop_reason": "done"}))
    lb = s.kernel_leaderboard()
    assert lb[0].run_id == "RUN_A"  # still running, sorts first
    assert {r.run_id for r in lb} == {"RUN_A", "RUN_B"}
    focused = s.focused_kernel_run()
    assert focused is not None and focused.run_id == "RUN_A"


def test_no_kernel_activity_by_default():
    s = PulseState()
    assert not s.has_kernel_activity()
    assert s.kernel_leaderboard() == []
    assert s.focused_kernel_run() is None
    assert s.curiosity_feed() == []


# ------------------------------------------------------- seed-baseline guard --
def test_seed_baseline_jump_not_starred_or_fed():
    """The first best event (delta == bf, the seed publish) is not a discovery:
    no jump spike, no Δ — but a later genuine improvement is."""
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": RID}))
    # Seed publish: improvement_delta equals the whole best_fitness (from 0).
    s.apply(ev("tsp.best_fitness_improved.v1", {
        "run_id": RID, "generation": 0, "best_fitness": 0.67,
        "improvement_delta": 0.67, "best_island": 0}))
    run = s.kernel_runs[RID]
    assert run.best_fitness == 0.67          # fitness still recorded
    assert run.last_delta == 0.0             # but not surfaced as a Δ
    assert [sp for sp in s.curiosity_feed() if sp.kind == "jump"] == []
    # A genuine later improvement (delta < bf) IS a discovery.
    s.apply(ev("tsp.best_fitness_improved.v1", {
        "run_id": RID, "generation": 3, "best_fitness": 0.71,
        "improvement_delta": 0.04, "best_island": 1}))
    jumps = [sp for sp in s.curiosity_feed() if sp.kind == "jump"]
    assert len(jumps) == 1 and jumps[0].starred is True
    assert s.kernel_runs[RID].last_delta == 0.04


# ----------------------------------------------------------- recency window --
def _ts(offset_s: float) -> str:
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 6, 2, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recency_window_prunes_stale_completed_runs():
    s = PulseState()
    # Stale completed run, far in the past relative to the fresh one.
    s.apply(ev("tsp.kernel.started.v1", {"run_id": "STALE"}, time=_ts(0)))
    s.apply(ev("tsp.kernel.completed.v1",
               {"run_id": "STALE", "total_generations": 3, "stop_reason": "done"},
               time=_ts(10)))
    # Fresh run, well beyond the recency window after the stale one.
    fresh_t = _ts(KERNEL_RUN_RECENCY_WINDOW_S + 600)
    s.apply(ev("sdf.kernel.started.v1", {"run_id": "FRESH"}, time=fresh_t))
    ids = {r.run_id for r in s.kernel_leaderboard()}
    assert ids == {"FRESH"}  # stale completed run pruned


def test_recency_window_keeps_running_run_however_old():
    s = PulseState()
    s.apply(ev("tsp.kernel.started.v1", {"run_id": "OLDRUN"}, time=_ts(0)))  # still running
    fresh_t = _ts(KERNEL_RUN_RECENCY_WINDOW_S + 600)
    s.apply(ev("sdf.kernel.completed.v1",
               {"run_id": "NEWDONE", "total_generations": 1, "stop_reason": "done"},
               time=fresh_t))
    ids = {r.run_id for r in s.kernel_leaderboard()}
    assert ids == {"OLDRUN", "NEWDONE"}  # running run never pruned by age
