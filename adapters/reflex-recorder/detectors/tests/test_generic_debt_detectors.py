"""tests/test_generic_debt_detectors.py — GenericStaleFenceDetector +
GenericDualSourceDetector + language packs + ProjectProfile mechanism.

Hermetic: each test builds a temp repo with synthetic source and asserts the
scanner's precision (catches the seeded debt, rejects the seeded noise) across
Rust/C, Go, and Python. Never touches a live tree.

Coverage:
  - language packs: Rust/C struct{}, Go type X struct{}, Python @dataclass/class
  - the profile mechanism: tengine *_addr fingerprint vs generic overlap
  - zero-config DEFAULT_PROFILE works with no project knowledge
  - engine hygiene: .claude/worktrees/ is skipped; findings dedup by content hash
  - stale_fence language + FIX anti-filter + named-alt across languages
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.stale_fence import (  # noqa: E402
    scan as fence_scan, is_stale_candidate, _STRONG_FENCE, _ANTI_FENCE,
)
from detectors.dual_source import scan as dual_scan  # noqa: E402
from detectors.stale_fence import is_stale_candidate as fence_is_candidate  # noqa: E402
from detectors.profiles import (  # noqa: E402
    ProjectProfile, StructOverlapStrategy, CommentRegexStrategy,
    MultiLineCommentStrategy, ScanContext, DEFAULT_PROFILE, TENGINE_PROFILE,
)
from detectors import langpacks  # noqa: E402


def _mk(tmp: Path, rel: str, body: str) -> None:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# ── language packs ─────────────────────────────────────────────────────────────

def test_langpack_c_struct():
    recs = dict((n, f) for n, f in langpacks.extract_all(textwrap.dedent("""\
        struct ExtGpuInfo {
            uint64_t camera_state_addr;
            uint64_t entity_system_addr;
        };
    """), ".h"))
    assert "ExtGpuInfo" in recs
    assert {"camera_state_addr", "entity_system_addr"} <= recs["ExtGpuInfo"]


def test_langpack_rust_struct():
    recs = dict(langpacks.extract_all(textwrap.dedent("""\
        pub struct GigaAddresses {
            pub camera_state_addr: u64,
            pub entity_system_addr: u64,
        }
    """), ".rs"))
    assert {"camera_state_addr", "entity_system_addr"} <= recs["GigaAddresses"]


def test_langpack_go_type_struct():
    """The probe proved `struct Name{}` misses Go — type X struct{} must parse."""
    recs = dict(langpacks.extract_all(textwrap.dedent("""\
        type SettlementScore struct {
            ContractID   string
            ScoreValue   float64
            StreamOffset int64
        }
    """), ".go"))
    assert "SettlementScore" in recs
    assert {"ContractID", "ScoreValue", "StreamOffset"} <= recs["SettlementScore"]


def test_langpack_python_dataclass():
    """Python @dataclass / class with annotated fields must parse."""
    recs = dict(langpacks.extract_all(textwrap.dedent("""\
        @dataclass
        class RouteRow:
            shard_key: str
            table_name: str
            dual_write: bool = False

            def method(self):
                local_var: int = 3
                return local_var
    """), ".py"))
    assert "RouteRow" in recs
    assert {"shard_key", "table_name", "dual_write"} <= recs["RouteRow"]
    # method-body locals must NOT leak in as fields
    assert "local_var" not in recs["RouteRow"]


# ── struct overlap works across all three languages (profile fingerprint) ───────

def test_struct_overlap_go(tmp_path):
    """Two Go structs sharing the project fingerprint fields fire."""
    profile = ProjectProfile(
        project="tachyonac", source_exts=(".go",),
        dual_source_fingerprints=(
            StructOverlapStrategy(name="score columns",
                                  field_pattern=r"_(score|stream)$", min_overlap=2),
        ),
    )
    _mk(tmp_path, "internal/a.go", """\
        type LiveScore struct {
            contract_score float64
            offset_stream  int64
            misc_a         int
        }
    """)
    _mk(tmp_path, "internal/b.go", """\
        type LegacyScore struct {
            contract_score float64
            offset_stream  int64
            misc_b         int
        }
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    overlaps = [h for h in hits if h.kind == "struct_overlap"]
    assert any("LiveScore" in h.anchor and "LegacyScore" in h.anchor for h in overlaps)


def test_struct_overlap_python(tmp_path):
    profile = ProjectProfile(
        project="deer-flow", source_exts=(".py",),
        dual_source_fingerprints=(StructOverlapStrategy(
            name="route columns", field_pattern=r".", min_overlap=3),),
    )
    _mk(tmp_path, "src/a.py", """\
        @dataclass
        class PrimaryRoute:
            shard_key: str
            table_name: str
            region_id: str
            primary_only: bool
    """)
    _mk(tmp_path, "src/b.py", """\
        @dataclass
        class ShadowRoute:
            shard_key: str
            table_name: str
            region_id: str
            shadow_only: bool
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    overlaps = [h for h in hits if h.kind == "struct_overlap"]
    assert any("PrimaryRoute" in h.anchor and "ShadowRoute" in h.anchor for h in overlaps)


# ── pluggable strategy interface admits NON-struct fingerprints ─────────────────

def test_comment_regex_strategy(tmp_path):
    """A CommentRegexStrategy fires on project-specific phrasing the generic
    dual-write scan wouldn't catch."""
    profile = ProjectProfile(
        project="tachyonac", source_exts=(".go",),
        dual_source_fingerprints=(
            CommentRegexStrategy(name="legacy-stream bridge",
                                 pattern=r"transitional dual-write|legacy stream"),
        ),
    )
    _mk(tmp_path, "internal/bridge.go", """\
        // Transitional dual-write: legacy stream NBUS_STREAM still fed here.
        func bridge() {}
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    assert any(h.kind == "dual_write" and "bridge.go" in h.anchor for h in hits)


def test_custom_channel_schema_strategy_admitted(tmp_path):
    """PROOF the interface admits a channel-vs-schema-file strategy (the
    nervous-bus dual-source shape) WITHOUT an engine edit — a sibling authors a
    DualSourceStrategy subclass whose find(ctx) globs schema files off
    ctx.repo_root and diffs them against publish-call channel strings."""
    from detectors.profiles import DualSourceStrategy, DualCandidate
    import re as _re

    class ChannelSchemaStrategy(DualSourceStrategy):
        name = "channel-vs-schema gap"
        _PUB = _re.compile(r'_publish\(\s*["\']([a-z0-9_.]+)["\']')

        def find(self, ctx):
            schemas = {p.stem.rsplit(".v", 1)[0]
                       for p in (ctx.repo_root / "schemas").glob("*.json")}
            out = []
            for rel, txt in ctx.texts:
                for ch in self._PUB.findall(txt):
                    if ch not in schemas:
                        out.append(DualCandidate(
                            kind="channel_schema_gap", anchor=ch,
                            detail=f"publish('{ch}') has no schema file",
                            evidence=[f"{rel}: emits '{ch}', no schemas/{ch}.v*.json"]))
            return out

    profile = ProjectProfile(
        project="nervous-bus", source_exts=(".py",),
        dual_source_fingerprints=(ChannelSchemaStrategy(),),
    )
    _mk(tmp_path, "schemas/kernel.session.v1.json", "{}")
    _mk(tmp_path, "src/producer.py", """\
        def run(self):
            self._publish("kernel.session", {})       # has schema -> ok
            self._publish("tsp.kernel.unmigrated", {}) # no schema -> gap
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    gaps = {h.anchor for h in hits if h.kind == "channel_schema_gap"}
    assert "tsp.kernel.unmigrated" in gaps
    assert "kernel.session" not in gaps


# ── the tengine *_addr fingerprint (reference profile) ──────────────────────────

def test_tengine_addr_fingerprint(tmp_path):
    """The validated tengine case: two structs sharing >=4 *_addr fields."""
    _mk(tmp_path, "crates/tengine-dgc-hal/csrc/a.h", """\
        struct ExtGpuInfo {
            uint64_t camera_state_addr;
            uint64_t entity_system_addr;
            uint64_t terrain_chunks_addr;
            uint64_t event_ring_addr;
            float delta_time;
        };
    """)
    _mk(tmp_path, "crates/tengine-dgc-hal/src/b.rs", """\
        pub struct GigaAddresses {
            pub camera_state_addr: u64,
            pub entity_system_addr: u64,
            pub terrain_chunks_addr: u64,
            pub event_ring_addr: u64,
            pub valid: u32,
        }
    """)
    hits = dual_scan(str(tmp_path), profile=TENGINE_PROFILE)
    overlaps = [h for h in hits if h.kind == "struct_overlap"]
    assert any("ExtGpuInfo" in h.anchor and "GigaAddresses" in h.anchor for h in overlaps)


def test_shared_scalars_not_dual_source(tmp_path):
    """Structs sharing only common scalars must NOT fire under *_addr fingerprint."""
    _mk(tmp_path, "crates/tengine-dgc-hal/c.h", """\
        struct WidgetA { float delta_time; uint frame_index; uint debug_flags; uint extra_a; };
        struct WidgetB { float delta_time; uint frame_index; uint debug_flags; uint extra_b; };
    """)
    hits = dual_scan(str(tmp_path), profile=TENGINE_PROFILE)
    assert not [h for h in hits if h.kind == "struct_overlap"]


# ── sync maps + dual-write + color-space anti-pattern ──────────────────────────

def test_sync_map_precision(tmp_path):
    """Sync-map detection is OPT-IN (TENGINE_PROFILE turns it on). When on, the
    X_TO_Y bridge fires but unit/enum/offset constants are excluded."""
    _mk(tmp_path, "crates/tengine-dgc-hal/csrc/maps.h", """\
        static const int SCHEMA_BUFFER_TO_EXTGPUINFO[8] = {0};
        #define RAD_TO_DEG 57.29578f
        #define VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE 2
        static const int GIGAHEADER_INIT_TIME_TO_LISTEN_MS_OFFSET = 40;
    """)
    hits = dual_scan(str(tmp_path), profile=TENGINE_PROFILE)
    maps = {h.anchor for h in hits if h.kind == "sync_map"}
    assert "SCHEMA_BUFFER_TO_EXTGPUINFO" in maps
    assert "RAD_TO_DEG" not in maps
    assert not any("CLAMP_TO_EDGE" in m for m in maps)
    assert not any(m.endswith("_OFFSET") for m in maps)


def test_sync_map_opt_in_off_by_default(tmp_path):
    """The probe proved sync maps are nearly all FP outside tengine, so the
    DEFAULT (and every non-tengine) profile emits ZERO sync_map hits even when a
    perfect X_TO_Y bridge name is present."""
    _mk(tmp_path, "src/maps.rs", """\
        const SCHEMA_BUFFER_TO_EXTGPUINFO: [u32; 8] = [0; 8];
    """)
    hits = dual_scan(str(tmp_path), profile=DEFAULT_PROFILE)
    assert not [h for h in hits if h.kind == "sync_map"]
    # but the SAME repo under a profile with sync_map_enabled=True DOES fire
    on = ProjectProfile(project="x", source_exts=(".rs",), sync_map_enabled=True)
    hits_on = dual_scan(str(tmp_path), profile=on)
    assert any(h.kind == "sync_map" and h.anchor == "SCHEMA_BUFFER_TO_EXTGPUINFO"
               for h in hits_on)


def test_colorspace_maps_not_sync(tmp_path):
    """hearth ANTI: color-space / pose maps are NOT hand-sync bridges — even with
    sync_map_enabled they must be excluded."""
    _mk(tmp_path, "src/color.rs", """\
        const LINEAR_SRGB_TO_DISPLAY: [[f32; 3]; 3] = [[0.0; 3]; 3];
        const BLAZEPOSE_TO_COCO: [usize; 17] = [0; 17];
        const ALPHA_TO_COVERAGE: u32 = 1;
        const SCHEMA_TABLE_TO_MIRROR_INDEX: [u32; 4] = [0; 4];
    """)
    on = ProjectProfile(project="x", source_exts=(".rs",), sync_map_enabled=True)
    hits = dual_scan(str(tmp_path), profile=on)
    maps = {h.anchor for h in hits if h.kind == "sync_map"}
    assert not any("SRGB" in m or "BLAZEPOSE" in m or "ALPHA_TO_COVERAGE" in m for m in maps)


def test_dual_write_comment_python(tmp_path):
    _mk(tmp_path, "src/router.py", """\
        # Dual-write during migration: keep the primary table and shadow in sync.
        def write(self): ...
    """)
    hits = dual_scan(str(tmp_path), profile=DEFAULT_PROFILE)
    assert any(h.kind == "dual_write" for h in hits)


# ── stale_fence across languages + anti-filter + named-alt ─────────────────────

def test_known_fence_strings_match():
    assert _STRONG_FENCE.search("Giga registry lookup was causing issues - stick with what works!")
    assert _STRONG_FENCE.search("DEPRECATED: Use get_camera_addr_giga(GigaHeader) instead")


def test_fix_annotations_not_fences():
    assert _ANTI_FENCE.search("// FIX: Use ALL_COMMANDS_BIT instead of BOTTOM_OF_PIPE_BIT")
    assert _ANTI_FENCE.search("// use this instead of raw access")


def test_stale_fence_python_named_alt(tmp_path):
    """A fence in a .py comment with a _v2 named-alt under DEFAULT_PROFILE."""
    _mk(tmp_path, "src/x.py", """\
        # DEPRECATED: use get_camera_addr_v2 instead
        def get_camera_addr(info):
            return info.camera_state_addr
        # FIX: use this instead of raw access
        def emit(self): ...
    """)
    hits = fence_scan(str(tmp_path), profile=DEFAULT_PROFILE, blame=False)
    texts = [h.text for h in hits]
    assert any("DEPRECATED" in t for t in texts)
    assert not any("raw access" in t for t in texts)  # FIX-anti rejected
    dep = [h for h in hits if "DEPRECATED" in h.text][0]
    assert dep.named_alt == "get_camera_addr_v2"
    assert is_stale_candidate(dep)


def test_stale_fence_go_comment(tmp_path):
    _mk(tmp_path, "internal/legacy.go", """\
        // DEPRECATED: use NewSettlementWriter instead
        func writeLegacy() {}
    """)
    profile = ProjectProfile(project="tachyonac", source_exts=(".go",),
                             twin_suffixes=("_new", "_v2", "_legacy"))
    hits = fence_scan(str(tmp_path), profile=profile, blame=False)
    assert any("DEPRECATED" in h.text for h in hits)


# ── engine hygiene: worktree skip + dedup ───────────────────────────────────────

def test_worktree_copies_skipped_and_deduped(tmp_path):
    """The same fence under .claude/worktrees/ must NOT multiply the finding."""
    fence = """\
        // DEPRECATED: use get_camera_addr_v2 instead
        def f(): ...
    """
    _mk(tmp_path, "src/world.py", fence)
    # 3 worktree copies of the same file — must all be skipped.
    _mk(tmp_path, ".claude/worktrees/wt-a/src/world.py", fence)
    _mk(tmp_path, ".claude/worktrees/wt-b/src/world.py", fence)
    _mk(tmp_path, ".worktrees/wt-c/src/world.py", fence)
    hits = fence_scan(str(tmp_path), profile=DEFAULT_PROFILE, blame=False)
    deprecated = [h for h in hits if "DEPRECATED" in h.text]
    # exactly one — the canonical src/world.py, not the worktree copies
    assert len(deprecated) == 1
    assert deprecated[0].file == "src/world.py"


# ── NEW: multi-line Go godoc dual-write strategy (tachyonac scorer.go shape) ────

def test_multiline_comment_strategy_go_godoc(tmp_path):
    """"writes to both X and\\n// Y" splits across two comment lines — the
    single-line scan misses it; MultiLineCommentStrategy joins the window."""
    profile = ProjectProfile(
        project="tachyonac", source_exts=(".go",),
        dual_source_fingerprints=(MultiLineCommentStrategy(name="multi-table write"),),
    )
    _mk(tmp_path, "internal/observatory/scorer.go", """\
        // ScoreSettlement reads our most recent estimate,
        // computes the Brier score, and writes to both contract_settlement_scores and
        // convergence_learning_records to close the loop.
        type Scorer struct {
            db int
        }
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    dw = [h for h in hits if h.kind == "dual_write" and "scorer.go" in h.anchor]
    assert dw, "multi-line writes-to-both must be caught"
    # anchor is the line where the match BEGINS (the 'writes to both' line)
    assert dw[0].anchor.endswith(":2")


def test_multiline_strategy_single_line_still_matches(tmp_path):
    """The same strategy also catches a fully single-line 'writes to both X and Y'
    when the second clause is on the NEXT comment line OR the same — here split."""
    profile = ProjectProfile(
        project="x", source_exts=(".go",),
        dual_source_fingerprints=(MultiLineCommentStrategy(name="w", lookahead=2),),
    )
    _mk(tmp_path, "a.go", """\
        // writes to both alpha_table and
        // beta_table for the migration window.
        func f() {}
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    assert any(h.kind == "dual_write" for h in hits)


def test_multiline_strategy_anti_veto(tmp_path):
    """A by-design fanout phrasing ('to both nbus:<ch> and nbus:all') is vetoed."""
    profile = ProjectProfile(
        project="x", source_exts=(".go",),
        dual_source_fingerprints=(MultiLineCommentStrategy(
            name="w", anti=r"nbus:all"),),
    )
    _mk(tmp_path, "pub.go", """\
        // writes it to both nbus:notify and
        // nbus:all as the designed fanout.
        func pub() {}
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    assert not [h for h in hits if h.kind == "dual_write"]


def test_comment_regex_strategy_anti_veto(tmp_path):
    """CommentRegexStrategy anti drops a fingerprint-matched but by-design line."""
    profile = ProjectProfile(
        project="x", source_exts=(".go",),
        dual_source_fingerprints=(CommentRegexStrategy(
            name="both-write", pattern=r"to both \w+", anti=r"nbus:all"),),
    )
    _mk(tmp_path, "a.go", """\
        // writes to both legacy_table and the new one  -> debt
        // writes it to both nbus:x and nbus:all          -> by-design
        func f() {}
    """)
    hits = dual_scan(str(tmp_path), profile=profile)
    anchors = [h.anchor for h in hits if h.kind == "dual_write"]
    assert any(a.endswith(":1") for a in anchors)      # debt line kept
    assert not any(a.endswith(":2") for a in anchors)  # fanout line vetoed


# ── NEW: named-replacement-file-exists fence refinement (hearth) ────────────────

def test_named_replacement_file_exists_required(tmp_path):
    """With require_named_replacement_file ON, a fence naming a PRESENT file fires;
    a fence naming an ABSENT file does NOT (lifts hearth precision 25%→~90%)."""
    profile = ProjectProfile(
        project="hearth", source_exts=(".rs",),
        require_named_replacement_file=True,
    )
    # present replacement -> real stale path
    _mk(tmp_path, "crates/v/src/pipeline.rs", """\
        //! DEPRECATED: Use engine.rs instead.
        pub fn old() {}
    """)
    _mk(tmp_path, "crates/v/src/engine.rs", "pub fn new_engine() {}\n")
    # absent replacement -> already resolved, must NOT fire
    _mk(tmp_path, "crates/w/src/old2.rs", """\
        //! DEPRECATED: Use gone_module.rs instead.
        pub fn old2() {}
    """)
    hits = fence_scan(str(tmp_path), profile=profile, blame=False)
    present = [h for h in hits if "pipeline.rs" in h.file]
    absent = [h for h in hits if "old2.rs" in h.file]
    assert present and present[0].named_file == "engine.rs"
    assert present[0].named_file_exists is True
    assert fence_is_candidate(present[0], require_named_replacement_file=True)
    # the absent-file fence is parsed but is NOT a candidate under the flag
    assert absent and absent[0].named_file_exists is False
    assert not fence_is_candidate(absent[0], require_named_replacement_file=True)
    # ...and WITHOUT the flag, the named-alt/later-fix base rule still governs
    # (engine.rs fence has no _v2 alt and blame=False so later_fixes=0 -> base False)
    assert not fence_is_candidate(present[0], require_named_replacement_file=False)


# ── NEW: test_file_globs exclusion ──────────────────────────────────────────────

def test_test_file_globs_exclude_dual_source(tmp_path):
    """Test files (nbus_test.go default + a profile-added name) are excluded from
    dual_source so synthetic test dual-writes don't inflate FP."""
    profile = ProjectProfile(
        project="tachyonac", source_exts=(".go",),
        dual_source_fingerprints=(CommentRegexStrategy(
            name="legacy", pattern=r"dual-write"),),
    )
    _mk(tmp_path, "internal/publisher.go", "// real dual-write bridge\nfunc f(){}\n")
    _mk(tmp_path, "internal/nbus_test.go", "// test dual-write fixture\nfunc T(){}\n")
    hits = dual_scan(str(tmp_path), profile=profile)
    files = {h.anchor.split(":")[0] for h in hits if h.kind == "dual_write"}
    assert "internal/publisher.go" in files
    assert not any("nbus_test.go" in f for f in files)


def test_test_file_globs_python_convention(tmp_path):
    """Python test_*_dual_write.py is excluded by the default test_ filename glob."""
    profile = ProjectProfile(project="deer-flow", source_exts=(".py",))
    _mk(tmp_path, "app/router.py", "# dual-write during migration\nx=1\n")
    _mk(tmp_path, "app/test_telemetry_router.py", "# dual-write fixture\ny=2\n")
    _mk(tmp_path, "app/test_code_graph_dual_write.py", "# dual-write fixture\nz=3\n")
    hits = dual_scan(str(tmp_path), profile=profile)
    files = {h.anchor.split(":")[0] for h in hits if h.kind == "dual_write"}
    assert "app/router.py" in files
    assert not any("test_" in f for f in files)


# ── NEW: dual_write_excludes vetoes generic scan + strategy candidates ──────────

def test_dual_write_excludes_vetoes_generic_scan(tmp_path):
    """A by-design phrasing matched by the generic _DUAL_WRITE scan is dropped when
    it matches dual_write_excludes (e.g. 'autoingest keeps them in sync')."""
    profile = ProjectProfile(
        project="kb", source_exts=(".rs",),
        dual_write_excludes=(r"autoingest`?\s+keeps\s+them\s+in\s+sync",),
    )
    _mk(tmp_path, "src/tier.rs", "// Keep the dedup index in sync so dups are caught.\nfn f(){}\n")
    _mk(tmp_path, "src/plugins.rs", "//! `kb autoingest` keeps them in sync.\nfn g(){}\n")
    hits = dual_scan(str(tmp_path), profile=profile)
    anchors = [h.anchor for h in hits if h.kind == "dual_write"]
    assert any("tier.rs" in a for a in anchors)        # real signal kept
    assert not any("plugins.rs" in a for a in anchors)  # designed-sync vetoed


def test_dual_write_excludes_vetoes_strategy_candidate(tmp_path):
    """The veto also applies to a STRATEGY-produced candidate, not just the
    generic scan (a class mention on a read-path line is dropped)."""
    profile = ProjectProfile(
        project="deer-flow", source_exts=(".py",),
        dual_source_fingerprints=(CommentRegexStrategy(
            name="router", pattern=r"\bDatabaseRouter\b"),),
        dual_write_excludes=(r"try\s+DatabaseRouter\s+first",),
    )
    _mk(tmp_path, "a.py", "# DatabaseRouter — dual-write during migration\nx=1\n")
    _mk(tmp_path, "b.py", "# Try DatabaseRouter first, then fall back to sqlite\ny=2\n")
    hits = dual_scan(str(tmp_path), profile=profile)
    anchors = [h.anchor for h in hits if h.kind == "dual_write"]
    assert any("a.py" in a for a in anchors)        # write-path mention kept
    assert not any("b.py" in a for a in anchors)    # read-path fallback vetoed


# ── NEW: dual_source worktree-copy dedup (Task 1a hermetic proof) ───────────────

def test_dual_source_worktree_copies_collapse_to_one(tmp_path):
    """A dual-write comment duplicated across two worktree-copy paths collapses to
    a single hit (skip_globs drops .claude/worktrees + .worktrees; content-hash
    dedup is belt-and-suspenders)."""
    body = "// keep the cache and the index in sync\nfunc f(){}\n"
    _mk(tmp_path, "internal/cache.go", body)
    _mk(tmp_path, ".claude/worktrees/wt-a/internal/cache.go", body)
    _mk(tmp_path, ".worktrees/wt-b/internal/cache.go", body)
    profile = ProjectProfile(project="x", source_exts=(".go",))
    hits = dual_scan(str(tmp_path), profile=profile)
    dw = [h for h in hits if h.kind == "dual_write"]
    assert len(dw) == 1
    assert dw[0].anchor.startswith("internal/cache.go:")


# ── NEW: Go parser handles LIVE-shape structs (tabs, tags, embedded fields) ──────

def test_langpack_go_live_shape(tmp_path):
    """Real Go: tab indent, struct tags, embedded field, pointer/slice types."""
    recs = dict(langpacks.extract_all(
        "type macroData struct {\n"
        "\tFedFundsRate  float64 `json:\"fed_funds_rate\"`\n"
        "\tDXY           float64\n"
        "\tFetchedAt     time.Time\n"
        "\tRates         []float64\n"
        "\tDB            *pgxpool.Pool\n"
        "}\n", ".go"))
    assert "macroData" in recs
    assert {"FedFundsRate", "DXY", "FetchedAt", "Rates", "DB"} <= recs["macroData"]


def test_zero_config_defaults_any_repo(tmp_path):
    """DEFAULT_PROFILE fires on a generic polyglot repo with no project knowledge."""
    _mk(tmp_path, "a.py", """\
        # keep the cache and the index in sync
        x = 1
    """)
    _mk(tmp_path, "b.go", """\
        // DEPRECATED: use NewThing instead
        func old() {}
    """)
    dual = dual_scan(str(tmp_path), profile=DEFAULT_PROFILE)
    fence = fence_scan(str(tmp_path), profile=DEFAULT_PROFILE, blame=False)
    assert any(h.kind == "dual_write" for h in dual)
    assert any("DEPRECATED" in h.text for h in fence)
