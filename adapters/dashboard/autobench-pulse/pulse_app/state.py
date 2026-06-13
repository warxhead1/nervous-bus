"""Domain state for autobench-pulse v2.

Single source of truth for the dashboard. Mutated only by the bus worker
(via ``call_from_thread``); read by widgets via reactive watchers.

Schemas consumed (see ``schemas/autobench.*.v1.json``):

  - ``autobench.phase.v1``     — phase boundary (benchmark / improver / commit / ...)
  - ``autobench.iteration.v1`` — RSI iteration start/complete, aggregate score
  - ``autobench.sandbox.v1``   — per-case sandbox verdict & latency
  - ``autobench.improver.v1``  — improver model call boundaries
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


def _rfc3339_epoch(ts: Optional[str]) -> float:
    """Parse an RFC3339 UTC timestamp to epoch seconds. 0.0 if unparseable."""
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0

# ---------------------------------------------------------------------------- #
# Constants                                                                    #
# ---------------------------------------------------------------------------- #

VALID_CHANNELS: frozenset[str] = frozenset(
    {
        # Core RSI loop boundaries
        "autobench.phase.v1",
        "autobench.iteration.v1",
        "autobench.sandbox.v1",
        "autobench.improver.v1",
        # Worker + per-case
        "autobench.worker.v1",
        "autobench.case.result.v1",
        # Failure pattern detector (CEPatternPanel)
        "autobench.failure_pattern.v1",
        # Worker queue-pressure signal (QueuePressureBar)
        "autobench.worker.queue_pressure.v1",
        # Iteration rollup (IterationProgressPanel + IterationLineageStrip)
        "autobench.iteration.summary.v1",
        # Budget guards (CostRatePanel)
        "autobench.budget.warning.v1",
        "autobench.budget.rate.v1",
        # Improver divergence + delta diff (DivergenceHighlights)
        "autobench.improver.divergence.v1",
        "autobench.improver.delta.diff.v1",
        # AHE prediction lifecycle (AHEPredictionTracker)
        "autobench.improver.prediction.v1",
        "autobench.improver.prediction.verified.v1",
        "autobench.improver.prediction.refuted_live.v1",
        # ane0: improver parse_status surface — distinguishes silent
        # parse fallback from explicit LLM-emitted no-change.
        "autobench.improver.reasoning.v1",
        # Sandbox stderr excerpts — feeds StderrFaultPanel (visual richness
        # fix 3 from pulse_visual_richness_exploration_2026-05-16). Channel
        # is emitted by autobench/observability.py:_sandbox_stderr but was
        # previously absent from this frozenset so events were dropped.
        "autobench.sandbox.stderr.v1",
        # Population-cycle summary (wire-pop Phase 1, nervous-bus-6yut). Names
        # the advocates + winner of a multi-advocate RSI cycle. Feeds the
        # MultiAdvocatePanel (nervous-bus-uwdq) — the cycle boundary that lets
        # the dashboard group iteration.v1 by per-advocate session_id and draw
        # N parallel trajectories.
        "autobench.population.summary.v1",
    }
)

# StderrFaultPanel — rolling ring of recent stderr excerpts (FIX 3).
STDERR_RING_SIZE: int = 5

# Rolling window for worker latency samples. 200 entries == roughly the last
# 200 worker calls, which at typical 10-20s/call is ~30-60 minutes of history.
WORKER_LATENCY_WINDOW: int = 200

# Verdicts that the FailureCodeSidebar surfaces. OK / WA / PASS are never pushed.
FAILURE_VERDICTS: frozenset[str] = frozenset({"CE", "RE", "TLE", "MLE"})

# How many failure cases the sidebar retains (oldest evicted FIFO).
FAILURE_RING_SIZE: int = 3

# Per-failure code preview length (first N chars of `generated_code`).
FAILURE_CODE_PREVIEW_CHARS: int = 200

# Cap on remembered failing-case records used for the fallback client-side
# detection path. Bounded so a long run can't blow memory.
FAILING_CASE_BUFFER: int = 256

# CostRatePanel — chart-history cap (§7.5.5). ~600 samples ≈ 10 min @ 1 Hz.
COST_HISTORY_MAX: int = 600

# Memory caps (perf — keep a long run / replay from growing without bound).
# ``sessions`` and ``iteration_summaries`` were previously never evicted; a
# multi-hour run or a full debug.jsonl replay would accumulate indefinitely.
# Keep the most-recent N and evict oldest. N is generous so the dashboard's
# recency-windowed views never notice the cap during normal operation.
MAX_SESSIONS: int = 200
MAX_ITERATION_SUMMARIES: int = 400
MAX_PREDICTIONS: int = 400

# Default dollar cap when no budget.warning event has been observed yet.
# Upgraded from the first budget.warning payload's max_cost_usd.
# NOTE: For MiniMax coding plan workloads dollars are NOTIONAL — the real
# budget unit is requests / 14250-per-5h. See feedback memory.
DEFAULT_MAX_COST_USD: float = 1.0

# IterationProgressPanel defaults
DEFAULT_CASES_PER_ITERATION: int = 20
LATENCY_WINDOW_CASES: int = 5
DEFAULT_ITER_OVERHEAD_S: float = 15.0

# DivergenceHighlights event kinds. Newer always displaces older.
DIVERGENCE_KIND_DELTA_DIFF = "delta_diff"
DIVERGENCE_KIND_DIVERGENCE = "divergence"

# QueuePressureBar — sparkline depth (samples retained for the bar's mini chart)
QUEUE_PRESSURE_WINDOW: int = 30

# IterationLineageStrip — number of columns rendered (N-3..N current + pending)
LINEAGE_STRIP_COLUMNS: int = 4

# Prediction-lifecycle states surfaced by AHEPredictionTracker. The values are
# string tags so the widget can map them to verdict colours; they do NOT match
# the verbose schema enum (which lives on the `verified` payload).
PREDICTION_STATUS_PENDING = "pending"
PREDICTION_STATUS_REFUTED_LIVE = "refuted_live"
PREDICTION_STATUS_CONFIRMED = "confirmed"
PREDICTION_STATUS_PARTIAL = "partial"
PREDICTION_STATUS_REFUTED = "refuted"

# nervous-bus-dq7l: pricing table REMOVED. Pulse never owned this number —
# it was multiplying tokens by hardcoded list prices that drift the moment
# any provider updates their rates, and worse, those rates don't apply at
# all to the MiniMax coding plan (requests-per-5h billing). Estimator now
# returns 0.0 unconditionally so no fabricated $ flows into the widgets.
DEFAULT_TOKEN_COSTS: dict[str, tuple[float, float]] = {}


def _estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """STUB — see nervous-bus-dq7l. Always returns 0.0.

    Pulse will not synthesize $ from token counts. The real billing unit
    for MiniMax is requests-per-5h; downstream widgets that previously
    showed $ now show 0 or are being migrated to request counts.
    """
    return 0.0


# ---------------------------------------------------------------------------- #
# Dataclasses                                                                  #
# ---------------------------------------------------------------------------- #


@dataclass
class FailureCase:
    """A single CE/RE/TLE/MLE case with a pre-truncated generated_code preview.

    Pre-aggregated in ``PulseState.apply`` so render-tick reads don't do any
    string slicing per frame (§7.5.5 / §8.2 — keep widget render() under 50ms).
    """

    case_id: str
    verdict: str  # one of FAILURE_VERDICTS
    iteration: int
    language: str
    p_score: float
    latency_ms: float
    code_preview: str  # already truncated to FAILURE_CODE_PREVIEW_CHARS
    code_truncated: bool  # True if the original was longer than the preview
    session_id: str
    seen_at: float = field(default_factory=time.time)


@dataclass
class IterationStats:
    """Per-iteration roll-up for a session."""

    iteration: int
    status: str = "start"
    aggregate_score: Optional[float] = None
    prev_score: Optional[float] = None
    # Numeric delta computed from prev_score → aggregate_score.
    score_delta: Optional[float] = None
    # Raw improvement_delta from the schema — an object describing the harness
    # change (system_prompt_delta, rollout_protocol_changed, etc.), NOT a number.
    improvement_delta: Optional[dict[str, Any]] = None
    verdict_counts: dict[str, int] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)
    active_phases: dict[str, float] = field(default_factory=dict)
    improver: Optional[dict[str, Any]] = None
    harness_version: str = ""
    bench_name: str = ""
    pareto_configs: Optional[int] = None
    # ---- progress tracking (nervous-bus-4cw9) ------------------------------
    # Wall clock when this iteration's start event was observed.
    started_at: float = field(default_factory=time.time)
    # Wall clock when iteration completed (None while in flight).
    completed_at: Optional[float] = None
    # Case-ids we've already counted for this iteration (dedup against the
    # case.result + sandbox streams which can both touch the same case).
    case_ids_done: set[str] = field(default_factory=set)
    # Per-case latency samples in arrival order (ms) — feeds rolling avg.
    case_latencies_ms: deque[float] = field(
        default_factory=lambda: deque(maxlen=LATENCY_WINDOW_CASES)
    )
    # Live verdict tally for the CURRENT iteration (distinct from
    # verdict_counts which is rolled up at iteration_complete).
    live_verdict_counts: dict[str, int] = field(default_factory=dict)
    # Total expected case count for this iteration. Discovered from the
    # iteration.summary.v1 event for prior iterations; defaults to
    # DEFAULT_CASES_PER_ITERATION until known.
    expected_num_cases: int = DEFAULT_CASES_PER_ITERATION


@dataclass
class DivergenceEvent:
    """One ribbon-worthy improver event, pre-aggregated for the widget.

    Sources:
        * ``autobench.improver.delta.diff.v1`` → ``kind="delta_diff"``
        * ``autobench.improver.divergence.v1`` → ``kind="divergence"``

    Stored on ``PulseState.latest_divergence_event``; the widget reads this
    blob each render tick and displays whichever event arrived most recently.
    Pre-aggregation lives in ``state.apply`` (not the widget) so the
    DivergenceHighlights widget stays sub-200ms per the §7.5.5 budget.
    """

    kind: str
    session_id: str
    iteration: int
    received_at: float = field(default_factory=time.time)
    # delta_diff fields ------------------------------------------------------
    system_prompt_diff: str = ""
    tool_surface_diff: str = ""
    rollout_protocol_change: Optional[dict[str, Any]] = None
    context_manager_change: Optional[dict[str, Any]] = None
    budget_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    no_change: bool = False
    # divergence fields ------------------------------------------------------
    divergent: bool = False
    divergence_summary: str = ""
    llm_delta: dict[str, Any] = field(default_factory=dict)
    heuristic_delta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PredictionRecord:
    """Lifecycle record for one AHE prediction.

    Predictions are keyed by ``(session_id, iteration)`` — the iteration the
    prediction was emitted FROM (i.e. iter N predicting iter N+1). The
    ``status`` evolves: pending → refuted_live (optional) → confirmed/partial/refuted.

    Attributes:
        session_id: Owning RSI session.
        iteration: Iteration that produced the prediction (predicts iter+1).
        confidence: Self-reported confidence in 0..1.
        predicted_score_delta: Predicted change in aggregate_score.
        predicted_verdict_class_changes: Predicted per-verdict count deltas.
        rationale: Improver's one-sentence justification.
        model: Improver model name.
        status: One of the ``PREDICTION_STATUS_*`` constants.
        actual_score_delta: Set when status moves out of pending/refuted_live.
        score_delta_error: ``|predicted - actual|`` (verified only).
        verdict_match_ratio: Fraction of predicted verdict shifts whose sign
            matched the actual sign (verified only).
        confidence_calibration: Calibration error (verified only).
        refutation_reason: Set when status is refuted_live.
        last_event_at: For ordering / recency.
    """

    session_id: str
    iteration: int
    confidence: float = 0.0
    predicted_score_delta: float = 0.0
    predicted_verdict_class_changes: dict[str, int] = field(default_factory=dict)
    rationale: str = ""
    model: str = ""
    status: str = "pending"
    actual_score_delta: Optional[float] = None
    score_delta_error: Optional[float] = None
    verdict_match_ratio: Optional[float] = None
    confidence_calibration: Optional[float] = None
    refutation_reason: str = ""
    last_event_at: float = field(default_factory=time.time)
    # Watermark value at the moment the prediction was first staked. Used as
    # the denominator for the AHE panel's draining thermometer bar (FIX 2 from
    # pulse_visual_richness_exploration_2026-05-16). ``None`` until the first
    # ``prediction_watermark`` call seeds it; once seeded it never decreases
    # below the initially-observed value so the bar has a stable maximum even
    # if mid-iteration predictions arrive with already-drawn slack.
    watermark_initial: Optional[int] = None


@dataclass
class Session:
    """One autobench RSI run."""

    session_id: str
    parent_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    last_event: float = field(default_factory=time.time)
    iterations: dict[int, IterationStats] = field(default_factory=dict)
    cost_usd: float = 0.0
    scores: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    # Total iterations expected for this session, if known via harness config.
    # ``None`` while unknown — the progress panel degrades to "iter N / ?".
    total_iterations: Optional[int] = None
    # Last expected_num_cases we learned from any iteration.summary.v1 event;
    # propagated as the default for fresh iterations within this session.
    last_known_num_cases: Optional[int] = None
    # ane0: latest improver parse_status (per-iteration). One of:
    # "ok" | "ok_after_repair" | "no_change" | "fell_back_to_rule_based"
    # | "parse_failed". None until the first reasoning event lands.
    last_improver_parse_status: Optional[str] = None
    last_improver_iteration: Optional[int] = None
    last_improver_fallback_reason: Optional[str] = None

    # -------------------------------------------------------------- helpers --
    @property
    def latest_iter(self) -> Optional[IterationStats]:
        if not self.iterations:
            return None
        return self.iterations[max(self.iterations)]

    @property
    def current_iter(self) -> Optional[IterationStats]:
        """The iteration the progress panel is interested in.

        Prefers an in-flight iteration (status == "start") over a completed
        one so the bar reflects "what's happening now" rather than the last
        rollup. Falls back to the most recent iteration if none are live.
        """
        if not self.iterations:
            return None
        live = [it for it in self.iterations.values() if it.status == "start"]
        if live:
            return max(live, key=lambda it: it.iteration)
        return self.latest_iter

    def rolling_avg_latency_ms(self, iter_num: Optional[int] = None) -> Optional[float]:
        """Mean of the last LATENCY_WINDOW_CASES per-case latencies (ms)."""
        if iter_num is None:
            it = self.current_iter
        else:
            it = self.iterations.get(iter_num)
        if it is None or not it.case_latencies_ms:
            return None
        return sum(it.case_latencies_ms) / float(len(it.case_latencies_ms))

    @property
    def latest_score(self) -> Optional[float]:
        it = self.latest_iter
        return it.aggregate_score if it else None

    @property
    def verdict(self) -> str:
        """Roll up an overall verdict label for the session."""
        it = self.latest_iter
        if it is None:
            return "pending"
        if it.status == "start":
            return "running"
        if it.score_delta is None:
            return "complete"
        if it.score_delta > 1e-9:
            return "improved"
        if it.score_delta < -1e-9:
            return "regressed"
        return "flat"

    def touch(self) -> None:
        self.last_event = time.time()


# ---------------------------------------------------------------------------- #
# FunSearch kernel evolution (the KernelArena)                                 #
# ---------------------------------------------------------------------------- #

# The kernels (tsp/sdf/sph/terrain/phase/thermal/latent/noise) emit a run-scoped
# event stream that is orthogonal to the RSI loop's session-scoped one. Channel
# names are NOT fixed strings — they're ``<kernel>.<event>.v1`` where the prefix
# is the kernel name — so we classify by suffix instead of a frozenset.

KERNEL_SPARK_WIDTH: int = 28          # cols of fitness sparkline per leaderboard row
KERNEL_HEATMAP_GENS: int = 26         # cols of island-health heatmap
KERNEL_ISLAND_HISTORY: int = 256      # per-island (gen, plateau, age) samples kept
KERNEL_FITNESS_HISTORY: int = 400     # per-run (gen, fitness) samples kept
KERNEL_SPIKE_RING: int = 48           # curiosity-feed ring size
# A best-fitness jump >= this is a "curiosity spike" (starred/bright in the feed).
KERNEL_SPIKE_THRESHOLD: float = 0.02
# Runs whose latest event is older than this (relative to the freshest kernel
# event seen) drop off the leaderboard/feed unless still running. Measured in
# event-time so it's correct on replay of an accumulated debug.jsonl, not just
# live. 30 min keeps the current + recently-active runs, sheds day-old tests.
KERNEL_RUN_RECENCY_WINDOW_S: float = 1800.0

# suffix (channel minus the ``<kernel>.`` prefix and ``.v1``) -> internal kind
_KERNEL_EVENT_KINDS: dict[str, str] = {
    "kernel.started": "started",
    "kernel.completed": "completed",
    "generation.completed": "generation",
    "best_fitness_improved": "best",
    "island_reset": "reset",
    "plateau_hint": "hint",
}

# Kernel-unification wave (2026-06-02 KERNEL_CONTRACT_SPEC §1): the 8 per-domain
# kernel families collapse into a single ``kernel.*`` prefix with the domain
# carried in ``data.domain``. We map the new channel suffix (channel minus the
# trailing ``.v1``) to the SAME internal kind the legacy ``<domain>.*`` events
# used, so both code paths converge on ``_apply_kernel_event``. Channels with no
# render meaning yet (candidate.evaluated, prior.loaded/updated) map to a benign
# ``ignore`` kind — recognised, counted, but not mutated into KernelRun.
_KERNEL_UNIFIED_KINDS: dict[str, str] = {
    "kernel.started": "started",
    "kernel.completed": "completed",
    "kernel.generation.completed": "generation",
    "kernel.best_fitness_improved": "best",
    "kernel.island_reset": "reset",
    "kernel.plateau_hint": "hint",
    "kernel.candidate.evaluated": "ignore",
    "kernel.prior.loaded": "ignore",
    "kernel.prior.updated": "ignore",
}

# Canonical 8-domain enum (KERNEL_CONTRACT_SPEC §1). Used to validate the
# ``data.domain`` discriminator on unified ``kernel.*`` events.
KERNEL_DOMAINS: frozenset[str] = frozenset(
    {"sph", "sdf", "noise", "phase", "terrain", "thermal", "latent", "tsp"}
)

# Low-frequency coalesced rollup (KERNEL_CONTRACT_SPEC §2). Consumed best-effort
# (a compact "snapshot seen" indicator); never the per-candidate firehose.
KERNEL_SNAPSHOT_CHANNEL: str = "pulse.kernel.snapshot.v1"


def classify_kernel_event(
    ev_type: Optional[str], data: Optional[dict[str, Any]] = None
) -> Optional[tuple[str, str]]:
    """Map a channel name to ``(kernel_name, kind)`` or ``None`` if not a kernel event.

    Handles BOTH contract shapes during the merge window (spec §3):

      * NEW unified ``kernel.*`` channels — the domain is read from
        ``data.domain`` (required field). If ``data`` is missing the domain we
        fall back to ``"?"`` and recover it later from the run's other events.
      * LEGACY ``<domain>.*`` channels — the domain is inferred from the
        channel prefix.

    The two shared, kernel-agnostic channels resolve to ``("?", ...)`` — the
    kernel name is recovered later from the run's prefixed events.
    """
    if not ev_type:
        return None
    if ev_type == "autobench.island.health.v1":
        return ("?", "island_health")
    if ev_type == "autobench.budget.gauge.v1":
        return ("?", "budget_gauge")
    if not ev_type.endswith(".v1"):
        return None
    body = ev_type[:-3]  # strip ".v1"

    # NEW unified ``kernel.*`` channels (spec §1). Domain comes from data.
    if body.startswith("kernel."):
        kind = _KERNEL_UNIFIED_KINDS.get(body)
        if kind is None:
            return None
        domain = "?"
        if isinstance(data, dict):
            d = data.get("domain")
            if isinstance(d, str) and d:
                domain = d
        return (domain, kind)

    head, _, rest = body.partition(".")
    # Anything under the autobench.* namespace is an RSI channel, not a kernel.
    if head == "autobench" or not rest:
        return None
    kind = _KERNEL_EVENT_KINDS.get(rest)
    if kind is None:
        return None
    return (head, kind)


@dataclass
class KernelRun:
    """One FunSearch kernel evolution run (keyed by ULID ``run_id``)."""

    run_id: str
    kernel: str = "?"
    instances: list[str] = field(default_factory=list)
    n_islands: int = 0
    population_per_island: int = 0
    target_generations: int = 0
    generation: int = 0
    best_fitness: float = 0.0
    mean_pop_fitness: float = 0.0
    best_island: int = -1
    llm_requests: int = 0
    requests_used: int = 0
    max_requests: Optional[int] = None
    code_diversity: float = 0.0
    fitness_std: float = 0.0
    status: str = "running"          # "running" | "complete"
    stop_reason: str = ""
    last_delta: float = 0.0
    n_resets: int = 0
    last_hint: str = ""
    final_code: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Event-time (epoch) of the latest applied event — used for recency filtering
    # that stays correct on replay of historical debug.jsonl. 0.0 = unknown.
    last_event_epoch: float = 0.0
    fitness_history: list[tuple[int, float]] = field(default_factory=list)
    mean_history: list[tuple[int, float]] = field(default_factory=list)
    # island_id -> latest {plateau_count, age, population_size, generation}
    island_health: dict[int, dict[str, Any]] = field(default_factory=dict)
    # island_id -> [(gen, plateau_count, age, best_fitness), ...] (capped)
    island_history: dict[int, list[tuple[int, int, int, float]]] = field(default_factory=dict)
    reset_gens: set[int] = field(default_factory=set)

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def fitness_values(self) -> list[float]:
        return [f for _, f in self.fitness_history]

    @property
    def label(self) -> str:
        inst = ",".join(self.instances) if self.instances else "?"
        return f"{self.kernel}·{inst}"


@dataclass
class Spike:
    """One notable moment in kernel evolution (the curiosity feed)."""

    ts: float
    kernel: str
    run_id: str
    generation: int
    kind: str               # jump | reset | hint | complete | start
    fitness: float = 0.0
    magnitude: float = 0.0  # improvement_delta for jumps
    detail: str = ""
    starred: bool = False    # a "curiosity spike" — a jump above threshold


# ---------------------------------------------------------------------------- #
# Aggregate state                                                              #
# ---------------------------------------------------------------------------- #


class PulseState:
    """All autobench state. Mutated by ``apply``; read by widgets."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.verdict_counts: defaultdict[str, int] = defaultdict(int)
        # (timestamp, $/min) samples for sparkline / gauge
        self.burn_window: deque[tuple[float, float]] = deque(maxlen=600)
        self.event_rate: deque[tuple[float, int]] = deque(maxlen=600)
        self.events_total: int = 0
        self.started_at: float = time.time()
        self._second_buckets: dict[int, int] = defaultdict(int)
        # Rolling window of worker.v1 latency_ms samples for the
        # WorkerLatencyHistogram widget. Bounded so the dashboard can run
        # indefinitely without growing.
        self.worker_latencies_ms: deque[float] = deque(maxlen=WORKER_LATENCY_WINDOW)
        # Pre-aggregated ring of recent CE/RE/TLE/MLE cases for the
        # FailureCodeSidebar. Bounded to FAILURE_RING_SIZE — oldest evicted FIFO
        # on every new failure. The widget reads (never writes) this deque
        # during its 10 Hz render tick.
        self.failure_cases: deque[FailureCase] = deque(maxlen=FAILURE_RING_SIZE)
        # Bumped each time a failure is pushed so the render tick can detect
        # "new failure" without comparing deque contents.
        self.failure_revision: int = 0
        # CE pattern panel data ------------------------------------------------
        # Most-recent failure_pattern.v1 event per (verdict, prefix) bucket.
        # When the detector fires we trust its sample_count outright.
        self.failure_patterns: dict[tuple[str, str], dict[str, Any]] = {}
        # Ring buffer of failing-case records, used to compute the panel client-
        # side before the detector has accumulated enough samples to emit. Each
        # entry: {"verdict","case_id","generated_code","iteration"}.
        self.failing_cases: deque[dict[str, Any]] = deque(maxlen=FAILING_CASE_BUFFER)

        # --- CostRatePanel state (bead nervous-bus-cewj) ---------------------
        # Running total cost across the session, summed from worker.v1.cost_usd
        # events. Distinct from per-session ``Session.cost_usd`` which uses the
        # improver-token-cost estimate; worker.v1 carries the *real* number.
        self.worker_cost_total_usd: float = 0.0
        # Trajectory of (timestamp, cumulative_cost_usd) for chart rendering.
        self.cost_history: deque[tuple[float, float]] = deque(maxlen=COST_HISTORY_MAX)
        # Dollar cap learned from budget.warning events; falls back to default.
        self.max_cost_usd: float = DEFAULT_MAX_COST_USD
        # True once a budget.warning event has populated max_cost_usd, so we
        # know the value is authoritative (not the default fallback).
        self.max_cost_usd_known: bool = False
        # Threshold-fired records: { 0.5: (ts, iter_hint), 0.8: ..., 1.0: ... }
        self.budget_thresholds_fired: dict[float, tuple[float, int]] = {}
        # Last rate-budget snapshot: {current, max, window_seconds, fraction}.
        self.rate_state: dict[str, Any] = {}
        # Most-recent ribbon event (delta.diff or improver.divergence). The
        # DivergenceHighlights widget displays ONE event at a time — newer
        # always displaces older. ``None`` means no event has fired yet
        # (widget shows its empty state).
        self.latest_divergence_event: Optional[DivergenceEvent] = None
        # AHE predictions, keyed by (session_id, iteration) — iter that emitted
        # the prediction (it predicts iter+1). Most recent insertions last.
        self.predictions: dict[tuple[str, int], PredictionRecord] = {}
        # Per-iteration final snapshots taken on the iteration.summary.v1
        # boundary (nervous-bus-2ktd). Lets the lineage strip / future
        # widgets show prior-iteration tallies after the running tally has
        # rolled into the next iteration. Bounded so a long run stays cheap.
        self.iteration_history: deque[dict[str, Any]] = deque(maxlen=200)
        # Latest queue-pressure snapshot (autobench.worker.queue_pressure.v1).
        # The QueuePressureBar reads this dict and the rolling tps window below.
        self.queue_pressure_latest: Optional[dict[str, Any]] = None
        # Rolling tps samples for the sparkline — newest last. Bounded to
        # QUEUE_PRESSURE_WINDOW so memory stays flat across long runs.
        self.queue_pressure_tps_window: deque[float] = deque(maxlen=QUEUE_PRESSURE_WINDOW)
        # Iteration rollups for the IterationLineageStrip — keyed by
        # (session_id, iteration). Stored as plain dicts (the strip never
        # mutates them) so the widget renders from a stable snapshot.
        self.iteration_summaries: dict[tuple[str, int], dict[str, Any]] = {}
        # FIX 3: rolling ring of recent stderr excerpts for the
        # StderrFaultPanel. Each entry is a plain dict with keys:
        # ``case_id``, ``verdict``, ``stderr_excerpt``, ``language``,
        # ``exit_code``, ``iteration``, ``seen_at``. Newest pushed to the
        # right; bounded to STDERR_RING_SIZE so memory stays flat.
        self.recent_stderr: deque[dict[str, Any]] = deque(maxlen=STDERR_RING_SIZE)
        # Revision counter — bumped each time a stderr event is appended so
        # the render tick can detect "new entry" without diffing the deque.
        self.stderr_revision: int = 0

        # --- FunSearch kernel arena (nervous-bus-tvfw) -----------------------
        # Run-scoped kernel evolution state, keyed by ULID run_id. Distinct
        # from RSI ``sessions`` — kernels emit their own ``<kernel>.*`` stream
        # with no session_id. Drives the KernelLeaderboard / IslandHeatmap.
        self.kernel_runs: dict[str, KernelRun] = {}
        # Freshest kernel event-time (epoch) seen, for replay-correct recency
        # filtering of the leaderboard/feed. 0.0 until the first timed event.
        self._kernel_latest_event_epoch: float = 0.0
        # Ring of notable evolution moments (fitness jumps, resets, hints,
        # completions) — newest pushed right. Feeds CuriositySpikeFeed.
        self.curiosity_spikes: deque[Spike] = deque(maxlen=KERNEL_SPIKE_RING)
        # Bumped on every spike push so the render tick can change-detect.
        self.kernel_spike_revision: int = 0
        # Kernel-unification wave (spec §2): most-recent coalesced snapshot +
        # a running count. Cheap surface for a "snapshot seen" indicator; the
        # dashboard's primary render path stays on the raw kernel.* stream.
        self.kernel_snapshot_latest: Optional[dict[str, Any]] = None
        self.kernel_snapshot_count: int = 0

        # --- Multi-advocate population cycle (nervous-bus-uwdq) --------------
        # Most-recent autobench.population.summary.v1 payload, normalised to a
        # plain dict. ``None`` until a cycle summary lands — single-advocate
        # runs never emit one, so the MultiAdvocatePanel stays hidden and the
        # dashboard renders exactly as before (backward compat). Holds the
        # cycle_id, the advocate roster (advocate_id → session_id + scores),
        # and the winner so the panel can group iteration trajectories by
        # per-advocate session_id and badge the winner.
        self.population_summary: Optional[dict[str, Any]] = None

        # --- ParetoScatter memo (perf) --------------------------------------
        # pareto_points() is O(n) over iteration_summaries and is called twice
        # a second by _tick_aggregates; pareto_classified() then runs an O(n²)
        # frontier pass. Both only change when an iteration.summary lands or a
        # session score updates, so we memoize on a monotonic version that the
        # relevant write paths bump. Cache invalidated lazily on read.
        self._pareto_version: int = 0
        self._pareto_cache_version: int = -1
        self._pareto_cache: list[tuple[float, float]] = []

    # ----------------------------------------------------------- ingestion --
    def apply(self, evt: dict[str, Any]) -> set[str]:
        """Apply one CloudEvents envelope, return set of dirty session_ids.

        Returns an empty set if the event is irrelevant (wrong type, no sid).
        """
        ev_type = evt.get("type")
        data = evt.get("data") or {}

        # Kernel-unification wave (spec §2): the low-frequency coalesced rollup.
        # Consumed best-effort — we record a compact snapshot indicator and
        # never let it drive the per-candidate render path. Handled before the
        # firehose classifier so it can't be mistaken for a kernel.* event.
        if ev_type == KERNEL_SNAPSHOT_CHANNEL:
            self._apply_kernel_snapshot(data)
            self.events_total += 1
            return set()

        # FunSearch kernel events are run-scoped (no session_id) and use dynamic
        # channel names. During the merge window we accept BOTH the legacy
        # ``<domain>.*`` channels (domain inferred from prefix) AND the new
        # unified ``kernel.*`` channels (domain read from ``data.domain``), so
        # the dashboard works regardless of which repo has merged (spec §3).
        kev = classify_kernel_event(ev_type, data)
        if kev is not None:
            # ``ignore`` kinds (candidate.evaluated, prior.loaded/updated) are
            # recognised + counted but have no render meaning yet — don't mutate
            # KernelRun state for them.
            if kev[1] != "ignore":
                self._apply_kernel_event(kev, data, evt.get("time"))
            self.events_total += 1
            return set()

        if ev_type not in VALID_CHANNELS:
            return set()

        # Budget channels are session-scoped via BudgetGuard.session_id, but
        # they don't represent an autobench RSI session; route them through
        # cost-tracking handlers that don't allocate a Session.
        if ev_type == "autobench.population.summary.v1":
            # Population-cycle boundary (wire-pop Phase 1). Records the cycle
            # membership (advocate_id → session_id) + winner so the
            # MultiAdvocatePanel can group iteration.v1 trajectories by
            # per-advocate session_id. The summary's own session_id is the
            # coordinator (``pop-<cycle_id>``), NOT an advocate — we do NOT
            # allocate a Session for it.
            self._apply_population_summary(data)
            self.events_total += 1
            return set()
        if ev_type == "autobench.budget.warning.v1":
            self._apply_budget_warning(data)
            self.events_total += 1
            return set()
        if ev_type == "autobench.worker.queue_pressure.v1":
            # Pressure is a session-scoped read-only signal — no Session row
            # mutation, just a snapshot + rolling tps for the bar widget.
            self._apply_queue_pressure(data)
            self.events_total += 1
            return set()
        if ev_type == "autobench.budget.rate.v1":
            self._apply_budget_rate(data)
            self.events_total += 1
            return set()
        if ev_type == "autobench.worker.v1":
            # Always do the PulseState-level rollup (cost + latency) — these
            # are session-agnostic. If a session_id is present we also fall
            # through to create a Session below; if not, we return early.
            self._apply_worker(data)
            self.events_total += 1
            if not data.get("session_id"):
                return set()
            # else: fall through to session-aware path below

        sid = data.get("session_id")
        # case.result events historically carry no session_id (per-case scope).
        # Tolerate that — fall back to "?" so the sidebar still surfaces them.
        # FIX 3: sandbox.stderr.v1 has session_id in its schema but the current
        # emit shape at autobench/observability.py:_sandbox_stderr does not
        # populate it. Tolerate the gap so the panel still renders.
        if not sid:
            if ev_type in ("autobench.case.result.v1", "autobench.sandbox.stderr.v1"):
                sid = "?"
            else:
                return set()

        sess = self.sessions.get(sid)
        if sess is None:
            sess = Session(session_id=sid, parent_id=data.get("parent_session_id"))
            self.sessions[sid] = sess
            # Cap the session map (perf) — a long run / full-log replay would
            # otherwise grow it without bound. Evict the least-recently-active
            # sessions (and their now-orphaned predictions) when over cap.
            if len(self.sessions) > MAX_SESSIONS:
                self._evict_oldest_sessions()
        sess.touch()
        self.events_total += 1
        now_sec = int(time.time())
        self._second_buckets[now_sec] += 1

        if ev_type == "autobench.iteration.v1":
            self._apply_iteration(sess, data)
        elif ev_type == "autobench.phase.v1":
            self._apply_phase(sess, data)
        elif ev_type == "autobench.sandbox.v1":
            self._apply_sandbox(sess, data)
        elif ev_type == "autobench.improver.v1":
            self._apply_improver(sess, data)
        elif ev_type == "autobench.worker.v1":
            # The PulseState-level rollup already happened above; here we
            # only need to touch session state. Currently nothing to do —
            # worker cost is intentionally not rolled into sess.cost_usd to
            # avoid double-counting with improver cost in the burn gauge.
            pass
        elif ev_type == "autobench.failure_pattern.v1":
            self._apply_failure_pattern(sess, data)
        elif ev_type == "autobench.case.result.v1":
            self._apply_case_result(sess, data)
        elif ev_type == "autobench.iteration.summary.v1":
            self._apply_iteration_summary(sess, data)
        elif ev_type == "autobench.improver.prediction.v1":
            self._apply_prediction(sess, data)
        elif ev_type == "autobench.improver.prediction.refuted_live.v1":
            self._apply_prediction_refuted_live(sess, data)
        elif ev_type == "autobench.improver.prediction.verified.v1":
            self._apply_prediction_verified(sess, data)
        elif ev_type == "autobench.improver.delta.diff.v1":
            self._apply_delta_diff(sess, data)
        elif ev_type == "autobench.improver.divergence.v1":
            self._apply_improver_divergence(sess, data)
        elif ev_type == "autobench.improver.reasoning.v1":
            self._apply_improver_reasoning(sess, data)
        elif ev_type == "autobench.sandbox.stderr.v1":
            self._apply_sandbox_stderr(sess, data)

        return {sid}

    def _evict_oldest_sessions(self) -> None:
        """Drop the least-recently-active sessions back down to MAX_SESSIONS.

        Also drops predictions / iteration_summaries owned only by evicted
        sessions so the auxiliary maps don't retain pointers to vanished runs.
        ``last_event`` (wall clock) orders recency; the most-recent MAX_SESSIONS
        are kept. Cheap — runs only when the cap is exceeded (rare).
        """
        excess = len(self.sessions) - MAX_SESSIONS
        if excess <= 0:
            return
        oldest = sorted(self.sessions.values(), key=lambda s: s.last_event)[:excess]
        dropped: set[str] = {s.session_id for s in oldest}
        for sid in dropped:
            self.sessions.pop(sid, None)
        if dropped:
            self.predictions = {
                k: v for k, v in self.predictions.items() if k[0] not in dropped
            }
            self.iteration_summaries = {
                k: v for k, v in self.iteration_summaries.items() if k[0] not in dropped
            }
            # The point cloud changed — invalidate the pareto memo.
            self._pareto_version += 1

    # ------------------------------------------------------- kernel arena ---
    def _push_spike(self, run: "KernelRun", gen: int, kind: str, *,
                    fitness: float = 0.0, magnitude: float = 0.0,
                    detail: str = "", starred: bool = False) -> None:
        self.curiosity_spikes.append(Spike(
            ts=time.time(), kernel=run.kernel, run_id=run.run_id,
            generation=gen, kind=kind, fitness=fitness, magnitude=magnitude,
            detail=detail, starred=starred,
        ))
        self.kernel_spike_revision += 1

    def _apply_kernel_event(self, kev: tuple[str, str], data: dict[str, Any],
                            event_time: Optional[str] = None) -> None:
        """Apply one FunSearch kernel event (run-scoped, no Session)."""
        kernel, kind = kev
        run_id = str(data.get("run_id") or "?")
        run = self.kernel_runs.get(run_id)
        if run is None:
            run = KernelRun(run_id=run_id, kernel=kernel)
            self.kernel_runs[run_id] = run
        if kernel != "?" and run.kernel == "?":
            run.kernel = kernel
        run.touch()
        epoch = _rfc3339_epoch(event_time)
        if epoch > 0.0:
            run.last_event_epoch = epoch
            if epoch > self._kernel_latest_event_epoch:
                self._kernel_latest_event_epoch = epoch
        gen = int(data.get("generation", run.generation) or 0)

        if kind == "started":
            # Don't clobber a known kernel name with "?" — a unified kernel.*
            # event whose ``data.domain`` was absent classifies as "?"; in that
            # case keep whatever we already inferred from sibling events.
            if kernel != "?":
                run.kernel = kernel
            run.instances = [str(x) for x in (data.get("instances") or [])]
            run.n_islands = int(data.get("n_islands") or 0)
            run.population_per_island = int(data.get("population_per_island") or 0)
            run.target_generations = int(data.get("generations") or 0)
            run.status = "running"
            self._push_spike(run, gen, "start",
                             detail=",".join(run.instances) or "?")
        elif kind == "generation":
            run.generation = gen
            run.best_fitness = float(data.get("best_fitness") or 0.0)
            run.mean_pop_fitness = float(data.get("mean_pop_fitness") or 0.0)
            run.best_island = int(data.get("best_island", run.best_island))
            run.llm_requests = int(data.get("llm_requests") or run.llm_requests)
            run.fitness_std = float(data.get("fitness_std") or 0.0)
            run.code_diversity = float(data.get("code_diversity") or 0.0)
            run.fitness_history.append((gen, run.best_fitness))
            run.mean_history.append((gen, run.mean_pop_fitness))
            if len(run.fitness_history) > KERNEL_FITNESS_HISTORY:
                del run.fitness_history[:-KERNEL_FITNESS_HISTORY]
            if len(run.mean_history) > KERNEL_FITNESS_HISTORY:
                del run.mean_history[:-KERNEL_FITNESS_HISTORY]
            if data.get("island_reset_fired"):
                run.reset_gens.add(gen)
        elif kind == "best":
            bf = float(data.get("best_fitness") or 0.0)
            delta = float(data.get("improvement_delta") or 0.0)
            run.best_fitness = bf
            run.best_island = int(data.get("best_island", run.best_island))
            run.generation = max(run.generation, gen)
            # The first "best" event of a run reports improvement_delta measured
            # from a zero baseline (delta == bf), i.e. the seed being published
            # as the first-ever best — not an evolved discovery. Don't surface
            # it as a Δ improvement or a (starred) curiosity spike; only record
            # the fitness. Detect it gen-agnostically so it's replay-safe.
            seed_baseline = bf > 0.0 and delta >= bf - 1e-9
            if not seed_baseline:
                run.last_delta = delta
                self._push_spike(run, gen, "jump", fitness=bf, magnitude=delta,
                                 starred=delta >= KERNEL_SPIKE_THRESHOLD,
                                 detail=f"best → {bf:.4f}")
        elif kind == "reset":
            run.n_resets += 1
            run.reset_gens.add(gen)
            nc = int(data.get("n_islands_culled") or 0)
            self._push_spike(run, gen, "reset",
                             fitness=float(data.get("pre_reset_best_fitness") or 0.0),
                             detail=f"culled {nc} island(s) on plateau")
        elif kind == "hint":
            hint = str(data.get("hint_preview") or "")
            run.last_hint = hint
            self._push_spike(run, gen, "hint",
                             fitness=float(data.get("best_fitness") or 0.0),
                             detail=hint[:90])
        elif kind == "completed":
            run.status = "complete"
            run.stop_reason = str(data.get("stop_reason") or "")
            run.generation = int(data.get("total_generations") or run.generation)
            run.llm_requests = int(data.get("llm_requests") or run.llm_requests)
            bp = data.get("best_program")
            if isinstance(bp, dict):
                run.best_fitness = float(bp.get("fitness") or run.best_fitness)
                code = bp.get("priority_code") or bp.get("code") or ""
                if code:
                    run.final_code = str(code)
            self._push_spike(run, run.generation, "complete", fitness=run.best_fitness,
                             detail=run.stop_reason[:60] or "complete")
        elif kind == "island_health":
            isl = int(data.get("island", -1))
            if isl >= 0:
                snap = {
                    "plateau_count": int(data.get("plateau_count") or 0),
                    "age": int(data.get("age_since_last_reset") or 0),
                    "population_size": int(data.get("population_size") or 0),
                    "best_fitness": float(data.get("best_fitness") or 0.0),
                    "generation": gen,
                }
                run.island_health[isl] = snap
                hist = run.island_history.setdefault(isl, [])
                hist.append((gen, snap["plateau_count"], snap["age"], snap["best_fitness"]))
                if len(hist) > KERNEL_ISLAND_HISTORY:
                    del hist[:-KERNEL_ISLAND_HISTORY]
                run.n_islands = max(run.n_islands, isl + 1)
                run.generation = max(run.generation, gen)
        elif kind == "budget_gauge":
            run.requests_used = int(data.get("requests_used") or run.requests_used)
            mr = data.get("max_requests")
            if mr is not None:
                run.max_requests = int(mr)
            run.generation = max(run.generation, gen)

    def _apply_kernel_snapshot(self, data: dict[str, Any]) -> None:
        """Record a ``pulse.kernel.snapshot.v1`` rollup (spec §2).

        Best-effort and deliberately cheap: we stash the latest payload and
        bump a counter so a compact "snapshot" indicator can surface it. The
        snapshot is a coalesced rollup, NOT the per-candidate firehose — we do
        NOT fan it into KernelRun state (that would double-count against the
        raw kernel.* stream when both are live).
        """
        if not isinstance(data, dict):
            return
        self.kernel_snapshot_latest = {
            "domain": str(data.get("domain") or "?"),
            "run_id": str(data.get("run_id") or "?"),
            "generation": data.get("generation"),
            "best_fitness": data.get("best_fitness"),
            "mean_fitness": data.get("mean_fitness"),
            "plateau": bool(data.get("plateau", False)),
            "event_count": data.get("event_count"),
            "received_at": time.time(),
        }
        self.kernel_snapshot_count += 1

    def kernel_snapshot_indicator(self) -> Optional[dict[str, Any]]:
        """Compact snapshot read for an optional indicator. ``None`` if unseen."""
        if self.kernel_snapshot_latest is None:
            return None
        out = dict(self.kernel_snapshot_latest)
        out["count"] = self.kernel_snapshot_count
        return out

    # ----- kernel accessors (read by the KernelArena widgets) ---------------
    def _is_recent_run(self, run: "KernelRun") -> bool:
        """True if a run should still appear (running, or recently active).

        Recency is measured in event-time relative to the freshest kernel event
        seen, so a replayed debug.jsonl full of day-old test runs is pruned the
        same as it would be live. Runs with no parseable event-time (epoch 0)
        are kept — never hide a run on a parse failure.
        """
        if run.is_running:
            return True
        latest = self._kernel_latest_event_epoch
        if latest <= 0.0 or run.last_event_epoch <= 0.0:
            return True
        return (latest - run.last_event_epoch) <= KERNEL_RUN_RECENCY_WINDOW_S

    def kernel_leaderboard(self, limit: int = 8) -> list[KernelRun]:
        """Active + recently-active runs, running first then newest. Capped."""
        runs = sorted(
            (r for r in self.kernel_runs.values() if self._is_recent_run(r)),
            key=lambda r: (r.is_running, r.updated_at),
            reverse=True,
        )
        return runs[:limit]

    def focused_kernel_run(self) -> Optional["KernelRun"]:
        """The run the heatmap focuses on.

        The heatmap needs island-health history to render anything, so we
        prefer (in order): the newest *running* run that has island history,
        then the newest run with island history, then just the newest run.
        Without this, the heatmap goes blank whenever the most-recently-updated
        run happens to lack island.health events (e.g. a just-completed run).
        """
        runs = list(self.kernel_runs.values())
        if not runs:
            return None
        with_islands = [r for r in runs if r.island_history]
        running = [r for r in runs if r.is_running]
        running_with = [r for r in with_islands if r.is_running]
        # Preference: running+islands → any+islands → running → any. This keeps
        # the heatmap on a live, island-rich run, but never blanks out: it
        # always falls back to the newest run of the best available tier.
        pool = running_with or with_islands or running or runs
        return max(pool, key=lambda r: r.updated_at)

    def curiosity_feed(self, n: int = 14) -> list[Spike]:
        """Newest-first slice of notable moments from recently-active runs."""
        recent_ids = {r.run_id for r in self.kernel_runs.values()
                      if self._is_recent_run(r)}
        spikes = [sp for sp in self.curiosity_spikes
                  if sp.run_id in recent_ids or sp.run_id not in self.kernel_runs]
        return spikes[-n:][::-1]

    def has_kernel_activity(self) -> bool:
        return bool(self.kernel_runs)

    def _apply_improver_reasoning(self, sess: Session, data: dict[str, Any]) -> None:
        """Record parse_status so the dashboard can distinguish silent parser
        fallback from LLM-emitted no-change. ane0."""
        status = data.get("parse_status")
        if isinstance(status, str) and status:
            sess.last_improver_parse_status = status
        it = data.get("iteration")
        if isinstance(it, int):
            sess.last_improver_iteration = it
        reason = data.get("fallback_reason")
        sess.last_improver_fallback_reason = reason if isinstance(reason, str) else None

    def _apply_case_result(self, sess: Session, data: dict[str, Any]) -> None:
        """Unified per-case handler. Drives THREE sub-states:

        1. ``failure_cases`` ring (FailureCodeSidebar): CE/RE/TLE/MLE only,
           with pre-aggregated code preview.
        2. ``failing_cases`` buffer (CEPatternPanel fallback): any non-OK
           verdict; raw generated_code retained for client-side prefix
           inference when the failure_pattern detector hasn't fired yet.
        3. Iteration progress (IterationProgressPanel): cases_done, latency
           samples, live verdict_counts on the current IterationStats.
        """
        verdict_raw = str(data.get("verdict") or "")
        verdict = verdict_raw.upper()
        case_id = str(data.get("case_id") or "?")
        try:
            iteration = int(data.get("iteration") or 0)
        except (TypeError, ValueError):
            iteration = 0

        # --- (1) FailureCodeSidebar ring ---------------------------------
        if verdict in FAILURE_VERDICTS:
            raw_code = data.get("generated_code") or ""
            raw_len = data.get("generated_code_length")
            try:
                original_len = int(raw_len) if raw_len is not None else len(raw_code)
            except (TypeError, ValueError):
                original_len = len(raw_code)
            preview = raw_code[:FAILURE_CODE_PREVIEW_CHARS]
            truncated = (
                original_len > FAILURE_CODE_PREVIEW_CHARS
                or len(raw_code) > FAILURE_CODE_PREVIEW_CHARS
            )
            try:
                p_score = float(data.get("p_score") or 0.0)
            except (TypeError, ValueError):
                p_score = 0.0
            try:
                latency_ms = float(data.get("latency_ms") or 0.0)
            except (TypeError, ValueError):
                latency_ms = 0.0
            fc = FailureCase(
                case_id=case_id,
                verdict=verdict,
                iteration=iteration,
                language=str(data.get("language") or ""),
                p_score=p_score,
                latency_ms=latency_ms,
                code_preview=preview,
                code_truncated=truncated,
                session_id=sess.session_id,
            )
            self.failure_cases.append(fc)
            self.failure_revision += 1

        # --- (2) CEPatternPanel client-side detection buffer -------------
        if verdict_raw and verdict_raw != "OK":
            self.failing_cases.append({
                "verdict": verdict_raw,
                "case_id": case_id,
                "generated_code": str(data.get("generated_code") or ""),
                "iteration": iteration,
            })

        # --- (3) IterationProgressPanel in-flight progress ---------------
        iter_num = data.get("iteration")
        if iter_num is None:
            it = sess.latest_iter
            if it is None:
                return
        else:
            try:
                it = self._ensure_iter(sess, int(iter_num))
            except Exception:
                return
        if not case_id or case_id in it.case_ids_done:
            return
        it.case_ids_done.add(case_id)
        latency = data.get("latency_ms")
        if isinstance(latency, (int, float)) and latency >= 0:
            it.case_latencies_ms.append(float(latency))
        if verdict_raw:
            it.live_verdict_counts[verdict_raw] = (
                it.live_verdict_counts.get(verdict_raw, 0) + 1
            )

    # ---- cost / rate channel handlers --------------------------------------
    def _apply_worker(self, sess_or_data: Any, data: Any = None) -> None:
        """Unified worker.v1 handler — does BOTH cost rollup AND latency capture.

        Accepts both call shapes the merge produced:
        - ``_apply_worker(data)`` — early-return path from ``apply()`` when no
          session is in scope. Captures cost + cost_history.
        - ``_apply_worker(sess, data)`` — main dispatch path with a session.
          Captures cost + latency. ``sess`` is currently informational; we do
          NOT roll worker cost into ``sess.cost_usd`` to avoid double-counting
          with improver cost in the burn gauge.
        """
        if data is None:
            # Single-arg call: sess_or_data is the data dict.
            data = sess_or_data
        # Cost rollup (CostRatePanel)
        try:
            usd = float(data.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            usd = None
        if usd is not None and usd >= 0:
            self.worker_cost_total_usd += usd
            self.cost_history.append((time.time(), self.worker_cost_total_usd))
        # Latency capture (WorkerLatencyHistogram)
        lat = data.get("latency_ms")
        if lat is not None:
            try:
                self.worker_latencies_ms.append(float(lat))
            except (TypeError, ValueError):
                pass

    def _apply_population_summary(self, data: dict[str, Any]) -> None:
        """Record a population-cycle summary (wire-pop Phase 1, nervous-bus-6yut).

        Stores the cycle membership (advocate roster keyed by advocate_id) plus
        the winner so the MultiAdvocatePanel (nervous-bus-uwdq) can use this as
        the cycle boundary and group iteration.v1 trajectories by per-advocate
        ``session_id``. Newest summary always displaces older — the dashboard
        shows the most-recent cycle. Malformed payloads (no advocates) are
        ignored so a partial emit can't blank the panel.
        """
        advocates_raw = data.get("advocates")
        if not isinstance(advocates_raw, list) or not advocates_raw:
            return
        advocates: list[dict[str, Any]] = []
        for adv in advocates_raw:
            if not isinstance(adv, dict):
                continue
            sid = adv.get("session_id")
            aid = adv.get("advocate_id")
            if not sid or not aid:
                continue
            try:
                final_score = float(adv.get("final_score"))
            except (TypeError, ValueError):
                final_score = None
            try:
                best_iter = int(adv.get("best_iter"))
            except (TypeError, ValueError):
                best_iter = None
            advocates.append(
                {
                    "advocate_id": str(aid),
                    "session_id": str(sid),
                    "final_score": final_score,
                    "best_iter": best_iter,
                }
            )
        if not advocates:
            return
        try:
            winner_score = float(data.get("winner_score"))
        except (TypeError, ValueError):
            winner_score = None
        self.population_summary = {
            "cycle_id": str(data.get("cycle_id") or ""),
            "session_id": str(data.get("session_id") or ""),
            "advocates": advocates,
            "winner_id": str(data.get("winner_id") or ""),
            "winner_score": winner_score,
            "cycle_started_at": data.get("cycle_started_at"),
            "cycle_ended_at": data.get("cycle_ended_at"),
        }

    def multi_advocate_view(self) -> Optional[dict[str, Any]]:
        """Snapshot for the MultiAdvocatePanel — N parallel advocate trajectories.

        Returns ``None`` when no population summary has landed OR when the most
        recent cycle named only a single advocate. Both cases mean the panel
        stays hidden and the dashboard renders exactly as a single-session run
        (backward compat, per nervous-bus-uwdq AC).

        When >1 advocate is present, returns a dict::

            {
              "cycle_id": str,
              "winner_id": str,
              "winner_score": float | None,
              "advocates": [
                {
                  "advocate_id": str,
                  "session_id": str,
                  "final_score": float | None,
                  "best_iter": int | None,
                  "is_winner": bool,
                  # live trajectory grouped from this advocate's iteration.v1
                  # events (NOT the summary) so the panel reflects in-flight
                  # progress, falling back to final_score when no live
                  # iterations have been seen for the advocate's session yet.
                  "scores": [float, ...],
                  "latest_iter": int | None,
                  "latest_score": float | None,
                },
                ...
              ],
            }

        Advocates are ordered by advocate_id for a stable layout; the winner is
        flagged via ``is_winner`` (the panel renders the badge).
        """
        summary = self.population_summary
        if not summary:
            return None
        advocates = summary.get("advocates") or []
        if len(advocates) <= 1:
            return None
        winner_id = summary.get("winner_id") or ""
        out_advocates: list[dict[str, Any]] = []
        for adv in sorted(advocates, key=lambda a: str(a.get("advocate_id"))):
            sid = adv.get("session_id")
            sess = self.sessions.get(sid) if sid else None
            # Group this advocate's live trajectory from its own session's
            # iteration.v1 scores (each advocate has its own session_id).
            scores: list[float] = []
            latest_iter: Optional[int] = None
            latest_score: Optional[float] = None
            if sess is not None:
                scores = list(sess.scores)
                li = sess.latest_iter
                if li is not None:
                    latest_iter = li.iteration
                    latest_score = li.aggregate_score
            # Fall back to the summary's final_score when no live iteration
            # data exists yet for this advocate (e.g. replay of just the
            # summary, or an advocate whose session events haven't arrived).
            if not scores and adv.get("final_score") is not None:
                scores = [float(adv["final_score"])]
            if latest_score is None:
                latest_score = adv.get("final_score")
            out_advocates.append(
                {
                    "advocate_id": adv.get("advocate_id"),
                    "session_id": sid,
                    "final_score": adv.get("final_score"),
                    "best_iter": adv.get("best_iter"),
                    "is_winner": adv.get("advocate_id") == winner_id,
                    "scores": scores,
                    "latest_iter": latest_iter,
                    "latest_score": latest_score,
                }
            )
        return {
            "cycle_id": summary.get("cycle_id") or "",
            "winner_id": winner_id,
            "winner_score": summary.get("winner_score"),
            "advocates": out_advocates,
        }

    def _apply_budget_warning(self, data: dict[str, Any]) -> None:
        """Record a threshold breach + recover ``max_cost_usd``.

        The first budget.warning payload tells us what the *real* dollar cap
        is for this run (typically $1.00). After that, threshold lines are
        drawn against the recovered value instead of the default fallback.
        """
        # Recover the authoritative cap once.
        try:
            cap = float(data.get("max_cost_usd") or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap > 0:
            self.max_cost_usd = cap
            self.max_cost_usd_known = True
        # Threshold record — keyed by the fractional threshold (0.5/0.8/1.0).
        try:
            threshold = float(data.get("threshold") or 0.0)
        except (TypeError, ValueError):
            return
        if threshold <= 0:
            return
        # iter_hint is a rough x-coord for the chart marker; the budget
        # payload doesn't carry an iteration number, so we use the current
        # cost-history length as a stand-in for the marker x.
        iter_hint = len(self.cost_history)
        self.budget_thresholds_fired[round(threshold, 3)] = (time.time(), iter_hint)

    def _apply_budget_rate(self, data: dict[str, Any]) -> None:
        """Snapshot the latest rate-budget readout for the secondary display."""
        try:
            current = int(data.get("current_count") or 0)
            max_req = int(data.get("max_requests") or 0)
            window = float(data.get("window_seconds") or 0.0)
            frac = float(data.get("fraction_used") or 0.0)
        except (TypeError, ValueError):
            return
        self.rate_state = {
            "current_count": current,
            "max_requests": max_req,
            "window_seconds": window,
            "fraction_used": frac,
        }

    # ---- divergence ribbon -------------------------------------------------
    def _apply_delta_diff(self, sess: Session, data: dict[str, Any]) -> None:
        """Stash the most-recent delta.diff event for the ribbon widget."""
        try:
            it_num = int(data.get("iteration", 0))
        except (TypeError, ValueError):
            it_num = 0
        rp = data.get("rollout_protocol_change")
        cm = data.get("context_manager_change")
        budget = data.get("budget_changes") or {}
        self.latest_divergence_event = DivergenceEvent(
            kind=DIVERGENCE_KIND_DELTA_DIFF,
            session_id=sess.session_id,
            iteration=it_num,
            system_prompt_diff=str(data.get("system_prompt_diff") or ""),
            tool_surface_diff=str(data.get("tool_surface_diff") or ""),
            rollout_protocol_change=rp if isinstance(rp, dict) else None,
            context_manager_change=cm if isinstance(cm, dict) else None,
            budget_changes=dict(budget) if isinstance(budget, dict) else {},
            no_change=bool(data.get("no_change", False)),
        )

    def _apply_improver_divergence(
        self, sess: Session, data: dict[str, Any]
    ) -> None:
        """Stash the most-recent improver.divergence event for the ribbon."""
        try:
            it_num = int(data.get("iteration", 0))
        except (TypeError, ValueError):
            it_num = 0
        llm = data.get("llm_delta") or {}
        heur = data.get("heuristic_delta") or {}
        self.latest_divergence_event = DivergenceEvent(
            kind=DIVERGENCE_KIND_DIVERGENCE,
            session_id=sess.session_id,
            iteration=it_num,
            divergent=bool(data.get("divergent", False)),
            divergence_summary=str(data.get("divergence_summary") or ""),
            llm_delta=dict(llm) if isinstance(llm, dict) else {},
            heuristic_delta=dict(heur) if isinstance(heur, dict) else {},
        )

    # ---- per-type ----------------------------------------------------------
    def _ensure_iter(self, sess: Session, num: int) -> IterationStats:
        it = sess.iterations.get(num)
        if it is None:
            it = IterationStats(iteration=num)
            sess.iterations[num] = it
        return it

    def _apply_iteration(self, sess: Session, data: dict[str, Any]) -> None:
        num = int(data.get("iteration", 0))
        it = self._ensure_iter(sess, num)
        status = data.get("status", it.status)
        prev_status = it.status
        it.status = status
        it.harness_version = data.get("harness_version", it.harness_version)
        # Propagate the last-known case count as the fresh iteration's
        # expected total, so the progress bar shows "0 / 20" not "0 / 0".
        if sess.last_known_num_cases:
            # Only overwrite if not already learned from a prior summary.
            if it.expected_num_cases == DEFAULT_CASES_PER_ITERATION:
                it.expected_num_cases = int(sess.last_known_num_cases)
        if status == "start":
            # Stamp the freshly-started iteration so ETA elapsed is honest.
            # If we somehow see start AFTER cases, leave the existing stamp.
            if prev_status != "start" or it.started_at == 0.0:
                it.started_at = time.time()
                # Reset progress trackers — same-iter restart cleans the bar.
                it.case_ids_done = set()
                it.case_latencies_ms = deque(maxlen=LATENCY_WINDOW_CASES)
                it.live_verdict_counts = {}
        if status == "complete":
            it.completed_at = time.time()
            prev_num = max((k for k in sess.iterations if k < num), default=None)
            if prev_num is not None and prev_num in sess.iterations:
                it.prev_score = sess.iterations[prev_num].aggregate_score
            it.aggregate_score = data.get("aggregate_score")
            it.verdict_counts = dict(data.get("verdict_counts") or {})
            raw_delta = data.get("improvement_delta")
            if isinstance(raw_delta, dict):
                it.improvement_delta = raw_delta
            it.pareto_configs = data.get("pareto_configs")
            if it.aggregate_score is not None:
                sess.scores.append(float(it.aggregate_score))
                if it.prev_score is not None:
                    it.score_delta = float(it.aggregate_score) - float(it.prev_score)
                # A new session score feeds the pareto back-compat path —
                # invalidate the memo.
                self._pareto_version += 1
            # global verdict counts
            for k, v in it.verdict_counts.items():
                self.verdict_counts[k] += int(v)

    def _apply_phase(self, sess: Session, data: dict[str, Any]) -> None:
        phase = data.get("phase", "?")
        status = data.get("status")
        it = sess.latest_iter or self._ensure_iter(sess, 0)
        if status == "start":
            it.active_phases[phase] = time.time()
            extra = data.get("extra") or {}
            if "bench_name" in extra:
                it.bench_name = extra["bench_name"]
        else:
            it.active_phases.pop(phase, None)

    def _apply_sandbox(self, sess: Session, data: dict[str, Any]) -> None:
        case_id = data.get("case_id", "?")
        status = data.get("status")
        it = sess.latest_iter or self._ensure_iter(sess, 0)
        existing = next((c for c in it.cases if c.get("case_id") == case_id), None)
        if existing is None:
            existing = {"case_id": case_id}
            it.cases.append(existing)
        existing["language"] = data.get("language", existing.get("language", ""))
        existing["sandbox_type"] = data.get("sandbox_type", existing.get("sandbox_type", ""))
        if status == "complete":
            existing["verdict"] = data.get("verdict")
            existing["latency_ms"] = data.get("latency_ms")
            existing["exit_code"] = data.get("exit_code")

    def _apply_sandbox_stderr(self, sess: Session, data: dict[str, Any]) -> None:
        """Push a sandbox stderr excerpt into the rolling ring (FIX 3).

        OK/WA never emit on this channel (the emitter gates on error-class
        verdicts), so every entry here is a CE/RE/TLE/MLE that the
        StderrFaultPanel should surface. The excerpt is already truncated
        to 200 chars upstream — we store it as-is.
        """
        verdict = str(data.get("verdict") or "").strip()
        if not verdict:
            return
        excerpt = str(data.get("stderr_excerpt") or "")
        try:
            iteration = int(data.get("iteration") or 0)
        except (TypeError, ValueError):
            iteration = 0
        exit_code = data.get("exit_code")
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        entry = {
            "case_id": str(data.get("case_id") or "?"),
            "verdict": verdict,
            "stderr_excerpt": excerpt,
            "language": str(data.get("language") or ""),
            "exit_code": exit_code,
            "iteration": iteration,
            "session_id": sess.session_id,
            "seen_at": time.time(),
        }
        self.recent_stderr.append(entry)
        self.stderr_revision += 1

    # ---- AHE prediction lifecycle -------------------------------------------
    def _apply_prediction(self, sess: Session, data: dict[str, Any]) -> None:
        """Create a new pending PredictionRecord from a prediction event."""
        try:
            iteration = int(data.get("iteration", 0))
        except (TypeError, ValueError):
            iteration = 0
        key = (sess.session_id, iteration)
        rec = PredictionRecord(
            session_id=sess.session_id,
            iteration=iteration,
            confidence=float(data.get("confidence") or 0.0),
            predicted_score_delta=float(data.get("predicted_score_delta") or 0.0),
            predicted_verdict_class_changes=dict(
                data.get("predicted_verdict_class_changes") or {}
            ),
            rationale=str(data.get("rationale") or ""),
            model=str(data.get("model") or ""),
            status=PREDICTION_STATUS_PENDING,
            last_event_at=time.time(),
        )
        self.predictions[key] = rec
        # Bound the prediction map (perf) — evict the oldest by last_event_at
        # when over cap. Generous N so the AHE tracker's recent view (≤5) and
        # the panel's history dots never lose anything they'd actually render.
        if len(self.predictions) > MAX_PREDICTIONS:
            for old_key in sorted(
                self.predictions, key=lambda k: self.predictions[k].last_event_at
            )[: len(self.predictions) - MAX_PREDICTIONS]:
                self.predictions.pop(old_key, None)

    def _apply_prediction_refuted_live(
        self, sess: Session, data: dict[str, Any]
    ) -> None:
        """Mark the matching pending prediction as live-refuted.

        The `iteration` field on the refuted_live payload is iter N+1, but the
        prediction was indexed by iter N — so we look up iter-1.
        """
        try:
            n_plus_1 = int(data.get("iteration", 0))
        except (TypeError, ValueError):
            n_plus_1 = 0
        # The prediction was emitted at iter N (N+1 - 1). Try that first; fall
        # back to walking recent iterations in case the indexing convention drifts.
        candidates = [n_plus_1 - 1, n_plus_1]
        rec: Optional[PredictionRecord] = None
        for cand in candidates:
            rec = self.predictions.get((sess.session_id, cand))
            if rec is not None:
                break
        if rec is None:
            # No matching prediction seen yet — synthesize one from the embedded
            # `prediction` block so the lifecycle is visible regardless.
            inner = data.get("prediction") or {}
            iteration = max(0, n_plus_1 - 1)
            key = (sess.session_id, iteration)
            rec = PredictionRecord(
                session_id=sess.session_id,
                iteration=iteration,
                confidence=float(inner.get("confidence") or 0.0),
                predicted_score_delta=float(inner.get("predicted_score_delta") or 0.0),
                predicted_verdict_class_changes=dict(
                    inner.get("predicted_verdict_class_changes") or {}
                ),
                rationale=str(inner.get("rationale") or ""),
                last_event_at=time.time(),
            )
            self.predictions[key] = rec
        if bool(data.get("is_refuted", True)):
            rec.status = PREDICTION_STATUS_REFUTED_LIVE
            rec.refutation_reason = str(data.get("refutation_reason") or "")
        rec.last_event_at = time.time()

    def _apply_prediction_verified(
        self, sess: Session, data: dict[str, Any]
    ) -> None:
        """Finalise a prediction with the verification outcome."""
        try:
            n_plus_1 = int(data.get("iteration", 0))
        except (TypeError, ValueError):
            n_plus_1 = 0
        candidates = [n_plus_1 - 1, n_plus_1]
        rec: Optional[PredictionRecord] = None
        for cand in candidates:
            rec = self.predictions.get((sess.session_id, cand))
            if rec is not None:
                break
        if rec is None:
            inner = data.get("predicted") or {}
            iteration = max(0, n_plus_1 - 1)
            key = (sess.session_id, iteration)
            rec = PredictionRecord(
                session_id=sess.session_id,
                iteration=iteration,
                confidence=float(inner.get("confidence") or 0.0),
                predicted_score_delta=float(inner.get("predicted_score_delta") or 0.0),
                predicted_verdict_class_changes=dict(
                    inner.get("predicted_verdict_class_changes") or {}
                ),
                rationale=str(inner.get("rationale") or ""),
                last_event_at=time.time(),
            )
            self.predictions[key] = rec
        outcome = str(data.get("outcome_label") or "refuted").lower()
        if outcome == "confirmed":
            rec.status = PREDICTION_STATUS_CONFIRMED
        elif outcome == "partial":
            rec.status = PREDICTION_STATUS_PARTIAL
        else:
            rec.status = PREDICTION_STATUS_REFUTED
        try:
            rec.actual_score_delta = float(data.get("actual_score_delta") or 0.0)
        except (TypeError, ValueError):
            rec.actual_score_delta = 0.0
        try:
            rec.score_delta_error = float(data.get("score_delta_error") or 0.0)
        except (TypeError, ValueError):
            rec.score_delta_error = 0.0
        try:
            rec.verdict_match_ratio = float(data.get("verdict_match_ratio") or 0.0)
        except (TypeError, ValueError):
            rec.verdict_match_ratio = 0.0
        try:
            rec.confidence_calibration = float(
                data.get("confidence_calibration") or 0.0
            )
        except (TypeError, ValueError):
            rec.confidence_calibration = 0.0
        rec.last_event_at = time.time()

    def _apply_improver(self, sess: Session, data: dict[str, Any]) -> None:
        it = sess.latest_iter or self._ensure_iter(sess, 0)
        imp = it.improver or {}
        imp["model"] = data.get("model", imp.get("model"))
        if data.get("status") == "start":
            imp["prompt_tokens"] = data.get("prompt_tokens", imp.get("prompt_tokens"))
        else:
            imp["completion_tokens"] = data.get("completion_tokens", imp.get("completion_tokens"))
            imp["delta_summary"] = data.get("delta_summary", imp.get("delta_summary"))
            # tally cost
            pt = int(imp.get("prompt_tokens") or 0)
            ct = int(imp.get("completion_tokens") or 0)
            model = imp.get("model") or ""
            usd = _estimate_cost_usd(model, pt, ct)
            sess.cost_usd += usd
            if usd > 0:
                self.burn_window.append((time.time(), usd))
        it.improver = imp

    def _apply_failure_pattern(self, sess: Session, data: dict[str, Any]) -> None:
        verdict = str(data.get("verdict") or "")
        prefix = str(data.get("prefix") or "")
        if not verdict:
            return
        sample_count = int(data.get("sample_count") or 0)
        if sample_count <= 0:
            return
        # Newer event for the same (verdict, prefix) overwrites — the detector
        # re-emits each iteration with refreshed counts, so last-write-wins is
        # the right policy. Iteration is retained for tie-break debugging.
        self.failure_patterns[(verdict, prefix)] = {
            "verdict": verdict,
            "prefix": prefix,
            "sample_count": sample_count,
            "total_in_class": int(data.get("total_in_class") or sample_count),
            "sample_case_ids": list(data.get("sample_case_ids") or []),
            "iteration": int(data.get("iteration") or 0),
            "source": "event",
        }

    def _apply_iteration_summary(self, sess: Session, data: dict[str, Any]) -> None:
        """Iteration rollup — most importantly, the authoritative num_cases.

        We use ``num_cases`` from the FIRST summary we see to lock in the
        expected total for subsequent iterations (the harness keeps it fixed
        across an RSI run). Also seeds the in-flight bar's denominator the
        first time the dashboard joins mid-run.

        Additionally caches the full summary in ``iteration_summaries`` so
        IterationLineageStrip can render the last N iterations as a horizontal
        strip without recomputing rollups from the case stream.
        """
        iter_num = int(data.get("iteration", 0))
        it = self._ensure_iter(sess, iter_num)
        num_cases = data.get("num_cases")
        if isinstance(num_cases, int) and num_cases > 0:
            it.expected_num_cases = int(num_cases)
            sess.last_known_num_cases = int(num_cases)
            # Also seed any in-flight (next) iteration so the next iter's bar
            # comes up with the right denominator on the first case.
            cur = sess.current_iter
            if cur is not None and cur.iteration != iter_num:
                if cur.expected_num_cases == DEFAULT_CASES_PER_ITERATION:
                    cur.expected_num_cases = int(num_cases)
        # Snapshot final tally to history (nervous-bus-2ktd). Prefer the
        # authoritative verdict_distribution from the summary payload; fall
        # back to the iteration's live tally if the schema is loose.
        dist = data.get("verdict_distribution") or data.get("verdict_counts")
        if not isinstance(dist, dict):
            dist = dict(it.live_verdict_counts)
        snapshot = {
            "session_id": sess.session_id,
            "iteration": iter_num,
            "num_cases": int(num_cases) if isinstance(num_cases, int) else len(it.case_ids_done),
            "verdict_counts": {str(k): int(v) for k, v in dist.items() if isinstance(v, (int, float))},
            "aggregate_score": data.get("aggregate_score"),
            "snapshot_at": time.time(),
        }
        self.iteration_history.append(snapshot)
        # Stash a lineage-friendly snapshot for IterationLineageStrip. Pure
        # dict — never mutated; widget renders from a stable view.
        # nervous-bus-sm8n: snapshot the cumulative worker-cost at this
        # iteration boundary so ParetoScatter can plot (cost, score) per
        # iteration without re-walking the cost_history deque.
        self.iteration_summaries[(sess.session_id, iter_num)] = {
            "session_id": sess.session_id,
            "iteration": iter_num,
            "aggregate_score": data.get("aggregate_score"),
            "verdict_distribution": dict(data.get("verdict_distribution") or {}),
            "received_at": time.time(),
            "cumulative_cost_usd": (
                self.worker_cost_total_usd + sess.cost_usd
            ),
        }
        # Bound the summary map (perf) — keep the most-recent N by insertion
        # order, evicting oldest. dict preserves insertion order in CPython, so
        # FIFO eviction is just popping the first key. Bounding here also bounds
        # the ParetoScatter input so its O(n)/O(n²) passes stay flat on a long
        # run / full-log replay.
        while len(self.iteration_summaries) > MAX_ITERATION_SUMMARIES:
            oldest = next(iter(self.iteration_summaries))
            del self.iteration_summaries[oldest]
        # Invalidate the pareto memo — a new summary changed the point cloud.
        self._pareto_version += 1

    # ---- queue pressure ----------------------------------------------------
    def _apply_queue_pressure(self, data: dict[str, Any]) -> None:
        """Snapshot the latest queue_pressure event + push tps onto the window.

        The bar widget reads ``queue_pressure_latest`` for the headline numbers
        and ``queue_pressure_tps_window`` for the inline sparkline. Both are
        re-derived each render tick.
        """
        try:
            cur = float(data.get("current_rate_tps") or 0.0)
        except (TypeError, ValueError):
            return
        try:
            base = float(data.get("baseline_tps") or 0.0)
        except (TypeError, ValueError):
            base = 0.0
        try:
            dev = float(data.get("deviation_factor") or 0.0)
        except (TypeError, ValueError):
            dev = 0.0
        self.queue_pressure_latest = {
            "current_rate_tps": cur,
            "baseline_tps": base,
            "deviation_factor": dev,
            "model": str(data.get("model") or ""),
            "recent_timeouts_count": int(data.get("recent_timeouts_count") or 0),
            "latest_latency_ms": float(data.get("latest_latency_ms") or 0.0),
            "received_at": time.time(),
        }
        self.queue_pressure_tps_window.append(cur)

    # ------------------------------------------------------------ queries --
    def throughput(self, window_s: int = 10) -> float:
        """Events per second, averaged over ``window_s`` seconds."""
        if not self._second_buckets:
            return 0.0
        now = int(time.time())
        total = sum(self._second_buckets.get(now - i, 0) for i in range(window_s))
        return total / float(window_s)

    def burn_rate_per_min(self, window_s: int = 60) -> float:
        """$/min over the last ``window_s`` seconds."""
        if not self.burn_window:
            return 0.0
        now = time.time()
        s = sum(amount for ts, amount in self.burn_window if now - ts <= window_s)
        # extrapolate to per-minute
        return s * (60.0 / window_s)

    def pareto_points(self) -> list[tuple[float, float]]:
        """(cost_usd, score) — one point per iteration boundary across all sessions.

        nervous-bus-sm8n: pre-change this returned ~1 point per session
        (the max score paired with the session-wide cost), which made the
        scatter look perpetually one-dotted no matter how many RSI
        iterations had landed. Now every iteration.summary snapshot
        contributes a (cumulative_cost_at_boundary, aggregate_score)
        tuple, so a 5-iter session yields 5 points and the frontier
        accumulates the way operators expect.

        Falls back to the old per-session view for sessions with scores
        but no iteration_summaries — preserves the smoke-test invariant
        in test_pareto_points_after_complete.

        Memoized (perf): the point cloud only changes when an iteration.summary
        lands or a session score updates — both bump ``_pareto_version``. Until
        then we hand back the cached list, so the 2 Hz aggregate tick (and the
        O(n²) frontier pass that consumes it) does no recompute on a quiescent
        scatter.
        """
        if self._pareto_cache_version == self._pareto_version:
            return self._pareto_cache
        out: list[tuple[float, float]] = []
        seen_sessions: set[str] = set()
        # Iteration-final snapshots first (the new, dense layer).
        for (sid, _iter), summary in self.iteration_summaries.items():
            score = summary.get("aggregate_score")
            cost = summary.get("cumulative_cost_usd")
            if score is None or cost is None:
                continue
            try:
                out.append((float(cost), float(score)))
                seen_sessions.add(sid)
            except (TypeError, ValueError):
                continue
        # Back-compat: any session with scores but no iteration_summaries
        # (older event streams, mid-run join) still surfaces as one point.
        for s in self.sessions.values():
            if s.session_id in seen_sessions:
                continue
            if s.scores:
                out.append((s.cost_usd, max(s.scores)))
        self._pareto_cache = out
        self._pareto_cache_version = self._pareto_version
        return out

    def pareto_classified(self) -> dict[str, list[tuple[float, float]]]:
        """Split ``pareto_points()`` into frontier vs dominated points.

        Frontier = the upper-right envelope: a point ``p`` is on the
        frontier when NO other point has BOTH ``score >= p.score`` AND
        ``cost <= p.cost`` (with at least one strict). Points that fail
        this are dominated.

        Returns ``{"frontier": [...], "dominated": [...]}``. Both lists
        carry plain ``(cost, score)`` tuples; the widget colors frontier
        in COLOR_AHE_HIT (green) and dominated in COLOR_DIM.
        """
        points = self.pareto_points()
        if not points:
            return {"frontier": [], "dominated": []}
        frontier: list[tuple[float, float]] = []
        dominated: list[tuple[float, float]] = []
        for i, p in enumerate(points):
            is_dominated = False
            for j, q in enumerate(points):
                if i == j:
                    continue
                # q dominates p if q has score >= p.score AND cost <= p.cost
                # with at least one strict inequality.
                if (
                    q[1] >= p[1]
                    and q[0] <= p[0]
                    and (q[1] > p[1] or q[0] < p[0])
                ):
                    is_dominated = True
                    break
            if is_dominated:
                dominated.append(p)
            else:
                frontier.append(p)
        return {"frontier": frontier, "dominated": dominated}

    def recent_predictions(self, limit: int = 5) -> list[PredictionRecord]:
        """Return the most recent ``limit`` predictions (newest last-event first).

        Newest first so callers can display chronologically (top = freshest) or
        flip the list for oldest-first layouts. The widget chooses; this method
        just orders by ``last_event_at``.
        """
        ordered = sorted(
            self.predictions.values(), key=lambda r: r.last_event_at, reverse=True
        )
        return ordered[:limit]

    def prediction_case_progress(
        self, prediction: PredictionRecord
    ) -> tuple[int, int]:
        """Best-effort (cases_done, cases_total) for a pending prediction.

        A Prediction emitted at iter N targets iter N+1. While we wait for that
        iteration's cases to land, the widget shows "X/Y done". Returns (0, 0)
        when we have no signal.
        """
        sess = self.sessions.get(prediction.session_id)
        if sess is None:
            return (0, 0)
        it = sess.iterations.get(prediction.iteration + 1)
        if it is None:
            return (0, 0)
        total = len(it.cases)
        done = sum(1 for c in it.cases if c.get("verdict"))
        return (done, total)

    def sessions_by_recency(self) -> list[Session]:
        return sorted(self.sessions.values(), key=lambda s: s.last_event, reverse=True)

    # ---- AHE prediction watermark & history --------------------------------
    def prediction_watermark(self, prediction: PredictionRecord) -> int:
        """How many iter N+1 cases remain before refutation becomes possible.

        Mirrors the conservative slack the refute_live detector uses: for each
        predicted verdict-class change, the per-class slack is
        ``predicted_count - actual_so_far_count``. The watermark is the
        smallest slack across all positive predicted entries; once it hits 0
        the prediction is one bad case away from being mathematically
        unreachable. When we have no live actuals we just report the smallest
        |predicted| change as the upper bound.
        """
        sess = self.sessions.get(prediction.session_id)
        live_iter: Optional[IterationStats] = None
        if sess is not None:
            live_iter = sess.iterations.get(prediction.iteration + 1)
        actuals: dict[str, int] = {}
        if live_iter is not None:
            actuals = dict(live_iter.live_verdict_counts)
        slacks: list[int] = []
        for verdict, predicted in (
            prediction.predicted_verdict_class_changes or {}
        ).items():
            try:
                pred_count = int(predicted)
            except (TypeError, ValueError):
                continue
            if pred_count <= 0:
                # Negative predictions ("expect 3 fewer CE") aren't directly
                # watermarkable — the case stream can always grow the actuals
                # in either direction. Skip; we'll fall back to absolute below.
                continue
            actual = int(actuals.get(verdict, 0))
            slacks.append(max(0, pred_count - actual))
        if slacks:
            watermark = min(slacks)
        else:
            # No positive predictions — fall back to smallest non-zero |predicted|
            # so the watermark still degrades gracefully toward 0 as actuals land.
            magnitudes = [
                abs(int(v)) for v in (prediction.predicted_verdict_class_changes or {}).values()
                if isinstance(v, (int, float))
            ]
            watermark = min(magnitudes) if magnitudes else 0
        # FIX 2: seed watermark_initial on the first observation so the panel's
        # thermometer bar has a stable denominator. Subsequent computations
        # only ratchet upward (never down) — if a fresh prediction arrives
        # with a larger absolute slack than we'd seen, take that as the new
        # max; otherwise keep the original. This avoids the bar appearing
        # "full" mid-drain just because actuals briefly receded.
        if prediction.watermark_initial is None:
            prediction.watermark_initial = max(0, int(watermark))
        elif watermark > prediction.watermark_initial:
            prediction.watermark_initial = int(watermark)
        return watermark

    def ahe_prediction_panel_payload(self) -> Optional[dict[str, Any]]:
        """Snapshot dict for the AHEPredictionPanel widget.

        Picks the most recent prediction (any session). Includes the
        computed watermark and history-dot row. Returns ``None`` when no
        prediction has been staked yet.

        nervous-bus-yn9v fix 6: history dots are scoped to the latest
        prediction's session by default so a single new prediction in a
        fresh session doesn't visually inherit 4 dots from prior cycles.
        ``history_dots_scope`` carries the count of dropped predictions
        so the widget can render a subtle "(N more across sessions)"
        annotation when state has older history available.
        """
        if not self.predictions:
            return None
        latest = max(self.predictions.values(), key=lambda r: r.last_event_at)
        session_dots = self.prediction_history_dots(session_id=latest.session_id)
        all_dots = self.prediction_history_dots()
        cross_session_count = max(0, len(all_dots) - len(session_dots))
        sess = self.sessions.get(latest.session_id)
        parse_status = sess.last_improver_parse_status if sess else None
        # FIX 2: compute watermark first so watermark_initial is seeded on
        # the record before we read it back into the payload.
        watermark = self.prediction_watermark(latest)
        return {
            "prediction": latest,
            "watermark": watermark,
            "watermark_initial": int(latest.watermark_initial or 0),
            "history_dots": session_dots,
            "history_dots_scope": {
                "session_id": latest.session_id,
                "cross_session_count": cross_session_count,
            },
            # ane0: parse_status badge — distinguishes silent parser failure
            # from explicit LLM-emitted "no change."
            "parse_status": parse_status,
        }

    def latest_completed_iteration_summary(self) -> Optional[dict[str, Any]]:
        """Most recent completed-iteration snapshot, or ``None``.

        nervous-bus-yn9v fix 3: surfaced so the CEPatternPanel empty state
        can fall back to a verdict-breakdown summary instead of rendering
        a near-empty box at the bottom of the right column.
        """
        if not self.iteration_history:
            return None
        # Sort by snapshot_at to honor recency irrespective of session order.
        snap = max(
            self.iteration_history,
            key=lambda h: float(h.get("snapshot_at") or 0.0),
        )
        return dict(snap)

    def improver_parse_status_summary(self) -> Optional[dict[str, Any]]:
        """Latest improver parse_status across all sessions, newest first.

        ane0: gives the dashboard a one-shot lookup for the parse-health badge
        — distinguishing "LLM said no change" from "parser silently failed."
        """
        candidates = [
            s for s in self.sessions.values()
            if s.last_improver_parse_status is not None
        ]
        if not candidates:
            return None
        sess = max(candidates, key=lambda s: s.last_event)
        return {
            "session_id": sess.session_id,
            "parse_status": sess.last_improver_parse_status,
            "iteration": sess.last_improver_iteration,
            "fallback_reason": sess.last_improver_fallback_reason,
        }

    def queue_pressure_summary(self) -> Optional[dict[str, Any]]:
        """Snapshot dict for the QueuePressureBar widget.

        Returns ``None`` until at least one queue_pressure event has arrived.
        """
        if self.queue_pressure_latest is None:
            return None
        return {
            "latest": dict(self.queue_pressure_latest),
            "tps_window": list(self.queue_pressure_tps_window),
        }

    def prediction_history_dots(
        self,
        limit: int = 5,
        session_id: Optional[str] = None,
    ) -> list[str]:
        """Return ``limit`` lifecycle dots, oldest→newest.

        Maps status → dot char. Used by the AHEPredictionPanel history strip.
        When ``session_id`` is supplied (the default for the panel), only
        predictions from that session are included — prevents cross-session
        history from leaking into a fresh cycle's first prediction.
        """
        records = list(self.predictions.values())
        if session_id is not None:
            records = [r for r in records if r.session_id == session_id]
        order = sorted(records, key=lambda r: r.last_event_at)
        out: list[str] = []
        for rec in order[-limit:]:
            if rec.status in ("confirmed",):
                out.append("●")
            elif rec.status == "partial":
                out.append("◐")
            elif rec.status in ("refuted", "refuted_live"):
                out.append("✗")
            elif rec.status == "pending":
                out.append("·")
            else:
                out.append("○")
        return out

    # ---- iteration lineage --------------------------------------------------
    def iteration_lineage(
        self, columns: int = LINEAGE_STRIP_COLUMNS
    ) -> list[dict[str, Any]]:
        """Return up to ``columns`` lineage-strip cells, oldest→newest.

        The rightmost cell is the in-flight (pending) iteration: it lifts the
        staked-prediction info onto the column when no completed summary
        exists yet. Earlier cells are completed iteration.summary rollups
        enriched with their prediction's outcome status (hit/miss/refuted/
        pending).
        """
        if not self.iteration_summaries and not self.predictions:
            return []
        # Pick the session whose most-recent activity drives the strip.
        recent = self.sessions_by_recency()
        if not recent:
            return []
        sess = recent[0]
        sid = sess.session_id
        # Completed iter summaries for this session, sorted by iteration.
        completed = sorted(
            (s for (sid_, _i), s in self.iteration_summaries.items() if sid_ == sid),
            key=lambda s: int(s.get("iteration") or 0),
        )
        # Most recent prediction emitted in this session (predicts iter+1).
        sess_preds = [
            r for r in self.predictions.values() if r.session_id == sid
        ]
        latest_pred = max(sess_preds, key=lambda r: r.last_event_at, default=None)

        # Decide whether a pending column will appear so we know how many
        # completed cells to reserve.
        pending_iter: Optional[int] = None
        predicted_delta: Optional[float] = None
        if latest_pred is not None:
            target_iter = latest_pred.iteration + 1
            # Only show pending column if we don't already have a completed
            # summary for that iteration.
            if (sid, target_iter) not in self.iteration_summaries:
                pending_iter = target_iter
                predicted_delta = latest_pred.predicted_score_delta
        completed_budget = columns - (1 if pending_iter is not None else 0)
        cells: list[dict[str, Any]] = []
        for summ in completed[-completed_budget:] if completed_budget > 0 else []:
            iter_num = int(summ.get("iteration") or 0)
            pred = self.predictions.get((sid, iter_num - 1))
            cells.append(
                {
                    "iteration": iter_num,
                    "kind": "completed",
                    "aggregate_score": summ.get("aggregate_score"),
                    "verdict_distribution": dict(summ.get("verdict_distribution") or {}),
                    "ahe_status": pred.status if pred else None,
                }
            )
        if pending_iter is not None:
            cells.append(
                {
                    "iteration": pending_iter,
                    "kind": "pending",
                    "aggregate_score": None,
                    "predicted_score_delta": predicted_delta,
                    "verdict_distribution": {},
                    "ahe_status": "pending",
                }
            )
        return cells[-columns:]

    def top_failure_patterns(self, n: int = 3) -> list[dict[str, Any]]:
        """Return top-N failure patterns by sample_count, descending.

        Prefers real ``autobench.failure_pattern.v1`` events when present. If
        none have fired yet but there are buffered failing case.result records,
        falls back to running the same detector client-side over the buffer
        with relaxed thresholds (threshold=2) so the panel is responsive in
        the first minute of a run. Each entry has the same shape as the
        ``failure_pattern.v1`` ``data`` block plus a ``source`` tag
        (``"event"`` or ``"inferred"``).
        """
        if self.failure_patterns:
            ranked = sorted(
                self.failure_patterns.values(),
                key=lambda p: (-int(p.get("sample_count") or 0), p.get("prefix") or ""),
            )
            return ranked[:n]
        if not self.failing_cases:
            return []
        return self._infer_failure_patterns(n=n)

    def _infer_failure_patterns(self, n: int = 3) -> list[dict[str, Any]]:
        """Client-side detector mirroring autobench.failure_pattern.

        Imported lazily so PulseState is usable without autobench installed
        (e.g. in stripped-down test environments). Returns the same shape as
        ``top_failure_patterns`` so the widget code is uniform.
        """
        try:
            from autobench.failure_pattern import detect_failure_patterns  # type: ignore
        except Exception:
            return self._infer_failure_patterns_local(n=n)

        # Wrap the deque entries in tiny objects the detector can consume.
        class _R:
            __slots__ = ("verdict", "metadata")

            def __init__(self, verdict: str, case_id: str, code: str) -> None:
                self.verdict = verdict
                self.metadata = {"case_id": case_id, "generated_code": code}

        wrapped = [_R(c["verdict"], c["case_id"], c["generated_code"]) for c in self.failing_cases]
        # Relaxed threshold=2 so the panel responds in the first minute.
        patterns = detect_failure_patterns(
            wrapped, prefix_len=20, threshold=2, max_prefixes_per_verdict=n
        )
        out: list[dict[str, Any]] = []
        for p in patterns:
            out.append({
                "verdict": getattr(p, "verdict", ""),
                "prefix": getattr(p, "prefix", ""),
                "sample_count": int(getattr(p, "sample_count", 0) or 0),
                "total_in_class": int(getattr(p, "total_in_class", 0) or 0),
                "sample_case_ids": list(getattr(p, "sample_case_ids", []) or []),
                "iteration": 0,
                "source": "inferred",
            })
        out.sort(key=lambda p: (-int(p["sample_count"]), p["prefix"]))
        return out[:n]

    def _infer_failure_patterns_local(self, n: int = 3) -> list[dict[str, Any]]:
        """Pure-local fallback used if autobench.failure_pattern is unimportable.

        Mirrors the detector's normalisation: lstrip then collapse newlines to
        ``|``. Threshold=2 to match the relaxed inference path.
        """
        per_verdict_total: dict[str, int] = defaultdict(int)
        buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
        for c in self.failing_cases:
            v = c.get("verdict") or ""
            if not v or v == "OK":
                continue
            per_verdict_total[v] += 1
            code = (c.get("generated_code") or "").lstrip()
            code = code.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "|")
            prefix = code[:20]
            buckets[(v, prefix)].append(c.get("case_id") or "")
        out: list[dict[str, Any]] = []
        for (v, prefix), ids in buckets.items():
            if len(ids) < 2:
                continue
            out.append({
                "verdict": v,
                "prefix": prefix,
                "sample_count": len(ids),
                "total_in_class": per_verdict_total[v],
                "sample_case_ids": list(ids[:5]),
                "iteration": 0,
                "source": "inferred",
            })
        out.sort(key=lambda p: (-int(p["sample_count"]), p["prefix"]))
        return out[:n]
    # ---- CostRatePanel queries ---------------------------------------------
    def cost_trajectory(self) -> tuple[list[float], list[float]]:
        """Return (xs, ys) of (elapsed_seconds_since_first_sample, cumulative_$).

        Empty session → ``([], [])``. The x-axis is elapsed seconds rather
        than wall-clock so the chart stays anchored at 0.
        """
        if not self.cost_history:
            return [], []
        t0 = self.cost_history[0][0]
        xs = [ts - t0 for ts, _ in self.cost_history]
        ys = [usd for _, usd in self.cost_history]
        return xs, ys

    def cost_summary(self) -> dict[str, Any]:
        """One-shot dict the CostRatePanel reads on every aggregate tick."""
        return {
            "total_usd": self.worker_cost_total_usd,
            "max_cost_usd": self.max_cost_usd,
            "max_cost_known": self.max_cost_usd_known,
            "thresholds_fired": dict(self.budget_thresholds_fired),
            "rate": dict(self.rate_state),
            "trajectory": self.cost_trajectory(),
        }

    def iteration_progress(
        self,
        session: Optional[Session] = None,
        iter_overhead_s: float = DEFAULT_ITER_OVERHEAD_S,
    ) -> Optional[dict[str, Any]]:
        """Snapshot the most relevant iteration progress for the dashboard.

        Returns ``None`` when no useful state exists yet. Otherwise returns
        a plain dict so the widget never holds a reference to mutating state.

        Per nervous-bus-4cw9, this is the IterationProgressPanel's input.
        """
        sess = session
        if sess is None:
            for candidate in self.sessions_by_recency():
                if candidate.iterations:
                    sess = candidate
                    break
        if sess is None:
            return None
        it = sess.current_iter
        if it is None:
            return None
        cases_done = len(it.case_ids_done)
        total = max(int(it.expected_num_cases or DEFAULT_CASES_PER_ITERATION), 1)
        avg_latency_ms = sess.rolling_avg_latency_ms(it.iteration)
        now = time.time()
        elapsed_s = max(0.0, now - it.started_at)
        # ETA: remaining cases * per-case avg + improver overhead, in seconds.
        if avg_latency_ms is not None and avg_latency_ms > 0:
            remaining = max(0, total - cases_done)
            eta_s: Optional[float] = remaining * (avg_latency_ms / 1000.0) + iter_overhead_s
        else:
            eta_s = None
        # nervous-bus-yn9v fix 1+2: when the iteration is complete, prefer
        # the authoritative iteration.summary.v1 rollup snapshot so the panel
        # renders "Iter N: 20/20 · score X · {OK: 16, ...}" instead of the
        # nonsensical "Cases: 0/20 100%" we got when case.result events were
        # filed under a different iteration index than the iteration event.
        history_snapshot: Optional[dict[str, Any]] = None
        if it.status == "complete":
            for h in reversed(self.iteration_history):
                if (
                    h.get("session_id") == sess.session_id
                    and int(h.get("iteration", -1)) == it.iteration
                ):
                    history_snapshot = h
                    break
        return {
            "session_id": sess.session_id,
            "iteration": it.iteration,
            "total_iterations": sess.total_iterations,
            "status": it.status,
            "cases_done": cases_done,
            "cases_total": total,
            "verdict_counts": dict(it.live_verdict_counts),
            "avg_case_latency_ms": avg_latency_ms,
            "elapsed_s": elapsed_s,
            "eta_s": eta_s,
            "started_at": it.started_at,
            "completed_at": it.completed_at,
            # Authoritative post-complete rollup (None while iter in flight).
            "history_snapshot": history_snapshot,
            # Persisted aggregate score (None until iteration_complete fires).
            "aggregate_score": it.aggregate_score,
        }

    def cycle_outcome_payload(self) -> Optional[dict[str, Any]]:
        """Snapshot dict for CycleOutcomeBanner (nervous-bus-wutr).

        Picks the most-recently-active session and rolls up:
          * session_id (full) + session_short (last 12 chars — matches
            the convention sysmap AutobenchPanel and SessionTree already
            use; the suffix is the random ULID entropy so two sessions
            won't collide visually).
          * verdict — improved / regressed / flat / running / complete /
            pending. Derived the same way ``Session.verdict`` is.
          * score_initial — the first iteration.summary aggregate_score.
          * score_final — the most recent aggregate_score (any iteration).
          * iters_count — number of iterations seen.
          * ahe_hits — count of PredictionRecord with status == "confirmed".
          * ahe_total — count of resolved (non-pending) predictions for
            this session.
          * cost_usd — cumulative spend for this session (worker total
            applies session-wide so we surface that — close enough; if
            multiple sessions ran concurrently the banner shows the
            highest-attention session's slice of total spend).

        Returns ``None`` when no session exists yet — the widget renders
        its empty state.
        """
        if not self.sessions:
            return None
        recent = self.sessions_by_recency()
        if not recent:
            return None
        sess = recent[0]
        # Initial vs final score from iteration summaries (authoritative).
        summary_keys = sorted(
            (k for k in self.iteration_summaries if k[0] == sess.session_id),
            key=lambda k: k[1],
        )
        score_initial: Optional[float] = None
        score_final: Optional[float] = None
        for k in summary_keys:
            sc = self.iteration_summaries[k].get("aggregate_score")
            if sc is None:
                continue
            try:
                sc_f = float(sc)
            except (TypeError, ValueError):
                continue
            if score_initial is None:
                score_initial = sc_f
            score_final = sc_f
        # Fall back to in-iteration scores deque when no summaries landed
        # yet (single-iter session that hasn't crossed a boundary).
        if score_final is None and sess.scores:
            score_initial = sess.scores[0]
            score_final = sess.scores[-1]
        # AHE roll-up — confirmed = hit; resolved = non-pending count.
        ahe_hits = 0
        ahe_total = 0
        for (sid_, _i), rec in self.predictions.items():
            if sid_ != sess.session_id:
                continue
            if rec.status == "pending":
                continue
            ahe_total += 1
            if rec.status == "confirmed":
                ahe_hits += 1
        # Total session spend — worker rollup is session-agnostic so we
        # report cumulative across all worker.v1 events plus this
        # session's improver-token estimate. Close enough; the banner is
        # a glance, not an audit.
        cost_usd = self.worker_cost_total_usd + sess.cost_usd
        return {
            "session_id": sess.session_id,
            "session_short": sess.session_id[-12:],
            "verdict": sess.verdict,
            "score_initial": score_initial,
            "score_final": score_final,
            "iters_count": len(sess.iterations),
            "ahe_hits": ahe_hits,
            "ahe_total": ahe_total,
            "cost_usd": cost_usd,
        }

    def summary_text(self) -> str:
        running = sum(1 for s in self.sessions.values() if s.verdict == "running")
        done = sum(1 for s in self.sessions.values() if s.verdict in ("improved", "regressed", "flat", "complete"))
        # nervous-bus-9l69 fix: the header used to sum ``s.cost_usd`` from
        # Session rows, but ``_apply_worker`` deliberately does NOT roll
        # worker cost into ``sess.cost_usd`` (to avoid double-counting with
        # the improver-token estimate in the burn gauge). So the header was
        # reading a field that's only populated for sessions that emit
        # ``autobench.improver.v1`` complete events — yielding 0.0000 in
        # production runs where the real dollars live in worker.v1 events.
        #
        # The authoritative cumulative spend is ``worker_cost_total_usd``
        # (the same field CostRatePanel reads via ``cost_summary()``), with
        # the improver-token estimate added back so a session with only
        # nervous-bus-dq7l: $ removed from the header. The MiniMax coding
        # plan bills by requests-per-5h (14250 cap), not dollars; pricing
        # tables are gone. Until the request-rate telemetry lands (dq7l
        # Phase 2), this surface intentionally shows no spend figure rather
        # than a fabricated one.
        return (
            f"sessions: {len(self.sessions)}  "
            f"running: {running}  done: {done}  "
            f"evt/s: {self.throughput():.1f}  "
            f"total: {self.events_total}"
        )


def _format_dollars_3sf(usd: float) -> str:
    """Format a dollar amount with ~3 significant figures.

    Per nervous-bus-9l69, the old ``:.4f`` formatter rendered 0.1111 as
    ``$: 0.1111`` (4 decimal places — fine) but 0.0000005 as ``$: 0.0000``
    which read as "no spend" even though the system was burning real money.
    This formatter keeps 3 sig figs across magnitudes so:

      * $0          → ``$0.000``
      * $0.0000005  → ``$5.00e-07``
      * $0.111      → ``$0.111``
      * $12.34      → ``$12.3``
      * $1234       → ``$1.23e+03``
    """
    if usd <= 0:
        return "$0.000"
    if usd < 1e-3:
        return f"${usd:.2e}"
    if usd < 1.0:
        return f"${usd:.3f}"
    if usd < 1000.0:
        return f"${usd:.3g}"
    return f"${usd:.2e}"
