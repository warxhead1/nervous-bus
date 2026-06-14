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
from detectors.profiles import (  # noqa: E402
    ProjectProfile, StructOverlapStrategy, CommentRegexStrategy,
    ScanContext, DEFAULT_PROFILE, TENGINE_PROFILE,
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
    _mk(tmp_path, "csrc/maps.h", """\
        static const int SCHEMA_BUFFER_TO_EXTGPUINFO[8] = {0};
        #define RAD_TO_DEG 57.29578f
        #define VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE 2
        static const int GIGAHEADER_INIT_TIME_TO_LISTEN_MS_OFFSET = 40;
    """)
    hits = dual_scan(str(tmp_path), profile=DEFAULT_PROFILE)
    maps = {h.anchor for h in hits if h.kind == "sync_map"}
    assert "SCHEMA_BUFFER_TO_EXTGPUINFO" in maps
    assert "RAD_TO_DEG" not in maps
    assert not any("CLAMP_TO_EDGE" in m for m in maps)
    assert not any(m.endswith("_OFFSET") for m in maps)


def test_colorspace_maps_not_sync(tmp_path):
    """hearth ANTI: color-space / pose maps are NOT hand-sync bridges."""
    _mk(tmp_path, "src/color.rs", """\
        const LINEAR_SRGB_TO_DISPLAY: [[f32; 3]; 3] = [[0.0; 3]; 3];
        const BLAZEPOSE_TO_COCO: [usize; 17] = [0; 17];
        const ALPHA_TO_COVERAGE: u32 = 1;
        const SCHEMA_TABLE_TO_MIRROR_INDEX: [u32; 4] = [0; 4];
    """)
    hits = dual_scan(str(tmp_path), profile=DEFAULT_PROFILE)
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
