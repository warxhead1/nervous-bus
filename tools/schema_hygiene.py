#!/usr/bin/env python3
"""
schema_hygiene.py — Schema Coverage Analyzer + Dead Letter Detective + Schema Updater

Analyzes nervous-bus schema coverage, identifies dead letter patterns, hypothesizes
schema gaps, and can update schemas to accept legitimate event variants that are
currently being rejected.

Usage:
    python3 schema_hygiene.py --report        # Print comprehensive markdown report
    python3 schema_hygiene.py --fix            # Write updated schemas to *_v2.json files
    python3 schema_hygiene.py --fix --dry-run  # Preview changes without writing files

Parts:
    1. Schema Coverage Analyzer     — Parse schemas, extract required fields + enum constraints
    2. Dead Letter Analyzer         — Read nbus:bus.dead_letter stream, group violations
    3. Schema Gap Hypothesizer      — Map violations → proposed schema updates with confidence
    4. Holistic Flow Analyzer       — Cross-reference schema events ↔ hearth-api handlers
    5. Tachyonos Deep Dive         — Analyze tachyonos.* flow from bus → hearth-api → DB
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import redis
except ImportError:
    redis = None


# ── Paths ───────────────────────────────────────────────────────────────────

SCHEMAS_DIR = Path(os.environ.get("NBUS_ROOT", Path(__file__).resolve().parents[1])) / "schemas"
HEARTH_NBUS_CONSUMER = Path(os.environ.get("NERVOUS_PROJECTS_BASE", Path.home() / "projects")) / "home-automation/crates/hearth-api/src/nbus_consumer.rs"
REDIS_STREAM_NBUS = "nbus:all"
REDIS_STREAM_DEAD_LETTER = "nbus:bus.dead_letter"
REDIS_URI = "redis://localhost:6379"


# ── Data Structures ──────────────────────────────────────────────────────────

class Confidence(Enum):
    HIGH = "high"       # Clear schema drift: new enum value needs adding
    MEDIUM = "medium"   # Likely drift with some uncertainty
    LOW = "low"        # Could be code bug; adding to schema might mask real issues
    UNCERTAIN = "uncertain"  # Not enough data to determine


@dataclass
class EnumField:
    name: str
    current_values: list[str]
    failed_values: list[str]


@dataclass
class SchemaSpec:
    path: Path
    name: str              # channel name (filename without .json)
    raw_event_type: str    # type field value from schema
    required: list[str]
    enum_fields: dict[str, list[str]]   # field_name → [allowed values]
    all_fields: list[str]


@dataclass
class DeadLetter:
    entry_id: str
    original_type: str
    original_source: str
    failure_reason: str
    violation_detail: str | None
    original_payload_excerpt: str | None
    channel: str | None    # For allowlist violations


@dataclass
class ViolationGroup:
    original_event_type: str
    violation_detail: str
    failure_reason: str = "schema_violation"
    field_name: str | None = None
    current_enum_values: list[str] = field(default_factory=list)
    failed_values: list[str] = field(default_factory=list)
    count: int = 0
    samples: list[DeadLetter] = field(default_factory=list)
    confidence: Confidence = Confidence.HIGH
    diagnosis: str = ""
    proposed_fix: str = ""
    json_patch: dict | None = None


@dataclass
class HandlerSpec:
    event_type_pattern: str  # e.g. "tachyonos.*" or "bus.notify.v1"
    handler_fn: str
    does_db_write: bool = False
    does_sse: bool = False
    does_notification: bool = False
    does_research_dispatch: bool = False
    does_file_write: bool = False
    notes: str = ""


# ── Part 1: Schema Coverage Analyzer ───────────────────────────────────────

def load_schemas() -> dict[str, SchemaSpec]:
    """Parse all JSON schemas from the schemas directory."""
    schemas = {}
    if not SCHEMAS_DIR.exists():
        print(f"WARNING: schemas directory not found at {SCHEMAS_DIR}", file=sys.stderr)
        return schemas

    for path in sorted(SCHEMAS_DIR.glob("*.json")):
        try:
            with open(path) as f:
                schema = json.load(f)
        except json.JSONDecodeError as e:
            print(f"WARNING: skipping {path.name}: {e}", file=sys.stderr)
            continue

        spec = parse_schema(path, schema)
        if spec:
            schemas[spec.name] = spec

    return schemas


def parse_schema(path: Path, schema: dict) -> SchemaSpec | None:
    """Extract relevant fields from a JSON Schema."""
    name = path.stem  # filename without .json

    # Get the canonical event type from the schema
    raw_event_type = None
    properties = schema.get("properties", {})

    # Many schemas use CloudEvents "type" const or data.type
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        # Check for "type" in properties (CloudEvent envelope)
        if "type" in props:
            type_prop = props["type"]
            if isinstance(type_prop, dict):
                if type_prop.get("const"):
                    raw_event_type = type_prop["const"]
                elif type_prop.get("enum"):
                    raw_event_type = type_prop["enum"][0]

        # Check data properties for type
        if "data" in props:
            data_props = props["data"].get("required", [])
            if "data" in data_props:
                pass

    # Extract required fields
    required = schema.get("required", [])
    if not required:
        # Try nested "data" required
        data = schema.get("properties", {}).get("data", {})
        if isinstance(data, dict):
            required = data.get("required", [])

    # Extract enum constraints on string fields
    enum_fields: dict[str, list[str]] = {}
    all_fields: list[str] = []

    def extract_enum_fields(props: dict, prefix: str = ""):
        for field_name, field_def in props.items():
            full_name = f"{prefix}{field_name}" if prefix else field_name
            all_fields.append(full_name)

            if isinstance(field_def, dict):
                if field_def.get("type") == "string" and "enum" in field_def:
                    enum_fields[full_name] = field_def["enum"]
                elif field_def.get("type") == "object":
                    extract_enum_fields(field_def.get("properties", {}), f"{full_name}.")

    extract_enum_fields(properties)
    if "data" in properties and isinstance(properties["data"], dict):
        extract_enum_fields(properties["data"].get("properties", {}), "data.")

    return SchemaSpec(
        path=path,
        name=name,
        raw_event_type=raw_event_type or name,
        required=required,
        enum_fields=enum_fields,
        all_fields=all_fields,
    )


# ── Part 2: Dead Letter Analyzer ───────────────────────────────────────────

def get_str(d: dict, k: str) -> str | None:
    """Fetch a string value from a dict that may have bytes or str keys/values."""
    v = d.get(k)
    if v is None:
        v = d.get(k.encode("utf-8"))
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return v if isinstance(v, str) else str(v)


def read_dead_letters(limit: int = 500) -> list[DeadLetter]:
    """Read all entries from the nbus:bus.dead_letter Redis stream."""
    dead_letters = []

    if redis is None:
        print("WARNING: redis python client not available", file=sys.stderr)
        return dead_letters

    try:
        client = redis.from_url(REDIS_URI)
        entries = client.xrange(REDIS_STREAM_DEAD_LETTER, count=limit)

        for entry_id, fields in entries:
            try:
                # redis returns bytes keys when using xrange; get_str handles both
                raw_payload = get_str(fields, "_raw")
                if not raw_payload:
                    continue

                # Parse the dead topic envelope
                envelope = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                data = envelope.get("data", {})

                original_type = None
                original_source = envelope.get("source", "")
                failure_reason = data.get("failure_reason", data.get("reason", "unknown"))
                violation_detail = data.get("schema_violation_detail")
                channel = data.get("channel")
                original_payload_excerpt = None

                # Parse the original event if present
                if "original_type" in data:
                    original_type = data["original_type"]
                elif "original_payload_excerpt" in data:
                    # Try to parse the excerpt to get type
                    try:
                        orig = json.loads(data["original_payload_excerpt"])
                        original_type = orig.get("type")
                        original_source = orig.get("source", original_source)
                        original_payload_excerpt = data["original_payload_excerpt"]
                    except json.JSONDecodeError:
                        original_payload_excerpt = data["original_payload_excerpt"]

                if not original_type:
                    # Fallback: parse the _raw directly
                    if isinstance(raw_payload, str):
                        try:
                            outer = json.loads(raw_payload)
                            original_type = outer.get("type")
                        except json.JSONDecodeError:
                            pass

                dead_letters.append(DeadLetter(
                    entry_id=entry_id,
                    original_type=original_type or "unknown",
                    original_source=original_source or "",
                    failure_reason=failure_reason,
                    violation_detail=violation_detail,
                    original_payload_excerpt=original_payload_excerpt,
                    channel=channel,
                ))

            except Exception as e:
                print(f"WARNING: failed to parse dead letter {entry_id}: {e}", file=sys.stderr)

        client.close()

    except redis.ConnectionError as e:
        print(f"WARNING: could not connect to Redis at {REDIS_URI}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: error reading dead letters: {e}", file=sys.stderr)

    return dead_letters


# ── Part 3: Schema Gap Hypothesizer ─────────────────────────────────────────

def extract_field_and_value(violation_detail: str) -> tuple[str | None, str | None]:
    """
    Parse a JSON Schema violation message to extract:
    - The field name that failed
    - The value that was sent but not allowed

    Examples:
        "'heartbeat' is not one of ['tool_call', 'tool_return', 'error']"
          → field="event", value="heartbeat"
        "'started' is not one of [...]"
          → field="event", value="started"
    """
    if not violation_detail:
        return None, None

    # Pattern: 'value' is not one of ['a', 'b', 'c']
    m = re.match(r"'([^']+)' is not one of \[\s*(.+?)\s*\]", violation_detail)
    if m:
        value = m.group(1)
        return value, value

    # Pattern: missing required property X
    m = re.search(r"missing required property '([^']+)'", violation_detail)
    if m:
        return f"(required: {m.group(1)})", None

    return None, None


def group_dead_letters(dead_letters: list[DeadLetter]) -> dict[tuple[str, str], ViolationGroup]:
    """Group dead letters by (original_event_type, violation_detail)."""
    groups: dict[tuple[str, str], ViolationGroup] = defaultdict(lambda: ViolationGroup(
        original_event_type="", violation_detail=""
    ))

    for dl in dead_letters:
        key = (dl.original_type, dl.violation_detail or "")
        if key not in groups:
            groups[key] = ViolationGroup(
                original_event_type=dl.original_type,
                violation_detail=dl.violation_detail or "",
            )
        groups[key].count += 1
        groups[key].samples.append(dl)

    return groups


def hypothesize_gaps(
    groups: dict[tuple[str, str], ViolationGroup],
    schemas: dict[str, SchemaSpec]
) -> list[ViolationGroup]:
    """
    For each violation group, determine:
    1. Which field failed
    2. What value was sent
    3. What the current schema allows
    4. Proposed fix with confidence rating
    """
    results = []

    for key, group in groups.items():
        violation_detail = group.violation_detail

        # Parse the violation to get field name and failed value
        if violation_detail:
            m = re.match(r"'([^']+)' is not one of", violation_detail)
            if m:
                group.field_name = "event"
                group.failed_values = [m.group(1)]

        # Look up the schema for this event type to find current enum
        schema_name = group.original_event_type
        if schema_name in schemas:
            schema_spec = schemas[schema_name]
            # Find enum values for the event field
            for field_name, enum_vals in schema_spec.enum_fields.items():
                if field_name in ["data.event", "event"]:
                    group.current_enum_values = enum_vals
                    if group.field_name is None:
                        group.field_name = field_name
                    break

        # Determine confidence and propose fix
        if group.failed_values and group.field_name:
            if group.field_name == "event" and group.original_event_type == "bus.agent.activity.v1":
                # Check if heartbeat is already in current schema (v0.3.0 added it)
                heartbeat_in_schema = "heartbeat" in group.current_enum_values
                started_in_schema = "started" in group.current_enum_values

                if "heartbeat" in group.failed_values and heartbeat_in_schema:
                    # Heartbeat is already in schema — this is a stale dead letter
                    group.confidence = Confidence.MEDIUM
                    group.diagnosis = (
                        "Stale dead letter: 'heartbeat' is in the current schema "
                        "(added in v0.3.0). This entry is from before the update."
                    )
                    group.proposed_fix = "No schema change — stale entry. Will expire from dead letter stream."
                elif "started" in group.failed_values:
                    group.confidence = Confidence.HIGH
                    group.diagnosis = (
                        "Codex agent emits event='started' when a session begins. "
                        "This value is not in the bus.agent.activity.v1 schema."
                    )
                    group.proposed_fix = (
                        "Add 'started' to the 'event' enum in bus.agent.activity.v1 schema. "
                        "Value is emitted by codex agents on session start."
                    )
                    group.json_patch = {
                        "op": "add",
                        "path": "/properties/event/enum/-",
                        "value": "started",
                    }
                else:
                    group.confidence = Confidence.HIGH
                    group.diagnosis = (
                        f"Schema drift: value {group.failed_values} is not in the "
                        f"allowed set for field '{group.field_name}'."
                    )
                    if group.current_enum_values:
                        new_vals = group.failed_values[:1]
                        group.proposed_fix = (
                            f"Add {new_vals} to the '{group.field_name}' enum"
                        )
                        group.json_patch = {
                            "op": "add",
                            "path": f"/properties/{group.field_name}/enum/-",
                            "value": new_vals[0],
                        }

        elif group.failure_reason == "allowlist_violation":
            group.confidence = Confidence.HIGH
            group.diagnosis = (
                "Allowlist violation: nbus-publish is filtering this channel because it's "
                "not in the allowlist. Either add to publish allowlist, or ignore if test."
            )
            group.proposed_fix = (
                f"Channel '{group.channel}' not in nbus-publish allowlist. "
                "Add to allowlist if legitimate, or ignore if test event."
            )

        results.append(group)

    return sorted(results, key=lambda g: g.count, reverse=True)


# ── Part 4: Holistic Flow Analyzer ─────────────────────────────────────────

def load_hearth_handlers() -> dict[str, HandlerSpec]:
    """Parse nbus_consumer.rs to build a map of event_type → handler spec."""
    handlers = {}

    if not HEARTH_NBUS_CONSUMER.exists():
        print(f"WARNING: nbus_consumer.rs not found at {HEARTH_NBUS_CONSUMER}", file=sys.stderr)
        return handlers

    content = HEARTH_NBUS_CONSUMER.read_text()

    # Known handlers with their capabilities
    handler_traits = {
        "handle_cycle_snapshot": HandlerSpec(
            event_type_pattern="deer-flow.cycle.snapshot",
            handler_fn="handle_cycle_snapshot",
            does_db_write=True,
            does_sse=True,
            notes="Writes cycle_results table; publishes HearthEvent::SessionUpdate",
        ),
        "handle_council_session": HandlerSpec(
            event_type_pattern="deer-flow.council.session",
            handler_fn="handle_council_session",
            does_db_write=True,
            does_sse=True,
            notes="Manages synthetic council sessions",
        ),
        "handle_run_lifecycle": HandlerSpec(
            event_type_pattern="deer-flow.run.",
            handler_fn="handle_run_lifecycle",
            does_db_write=True,
            does_sse=True,
            notes="Manages synthetic run sessions",
        ),
        "handle_research_cycle_completed": HandlerSpec(
            event_type_pattern="deer-flow.research.cycle.completed.v1",
            handler_fn="handle_research_cycle_completed",
            does_db_write=True,
            does_sse=True,
            does_notification=True,
            does_file_write=True,
            does_research_dispatch=True,
            notes="Writes KB article, upserts cycle_results, injects Ember insight, sets dedup key",
        ),
        "handle_loom_lifecycle_pr": HandlerSpec(
            event_type_pattern="loom.lifecycle.pr.v1",
            handler_fn="handle_loom_lifecycle_pr",
            does_sse=True,
            notes="Handles loom PR lifecycle events",
        ),
        "handle_loom_lifecycle": HandlerSpec(
            event_type_pattern="loom.lifecycle",
            handler_fn="handle_loom_lifecycle",
            does_notification=True,
            notes="Generic loom lifecycle handler",
        ),
        "handle_session_linked": HandlerSpec(
            event_type_pattern="agent.session.linked.v1",
            handler_fn="handle_session_linked",
            does_db_write=True,
            notes="Writes session link to DB",
        ),
        "handle_bus_notify": HandlerSpec(
            event_type_pattern="bus.notify.v1",
            handler_fn="handle_bus_notify",
            does_notification=True,
            notes="Routes notifications to phone/discord/ntfy",
        ),
        "handle_tengine_screenshot": HandlerSpec(
            event_type_pattern="bus.tengine.screenshot.captured.v1",
            handler_fn="handle_tengine_screenshot",
            does_sse=True,
            notes="Surfaces TEngine screenshots to SSE",
        ),
        "handle_tengine_silo_crash": HandlerSpec(
            event_type_pattern="bus.tengine.silo.crash.v1",
            handler_fn="handle_tengine_silo_crash",
            does_sse=True,
            notes="Surfaces crash events to SSE",
        ),
        "handle_tengine_session_event": HandlerSpec(
            event_type_pattern="tengine.session.",
            handler_fn="handle_tengine_session_event",
            does_sse=True,
            notes="Manages TEngine session state in Sessions tab",
        ),
        "handle_tengine_agent_activity": HandlerSpec(
            event_type_pattern="bus.agent.activity.v1",
            handler_fn="handle_tengine_agent_activity",
            does_db_write=True,
            notes="Persists TEngine agent activity to nbus_events",
        ),
        "handle_intrinsic_marker": HandlerSpec(
            event_type_pattern="bus.intrinsic.marker",
            handler_fn="handle_intrinsic_marker",
            notes="Handles health monitoring markers",
        ),
        "handle_forge_event": HandlerSpec(
            event_type_pattern="deer-flow.forge.",
            handler_fn="handle_forge_event",
            does_sse=True,
            notes="Handles deer-flow forge events",
        ),
        "handle_session_started": HandlerSpec(
            event_type_pattern="bus.hearth.session.started.v1",
            handler_fn="handle_session_started",
            does_sse=True,
            notes="Bridges session lifecycle to SSE",
        ),
        "handle_session_activity": HandlerSpec(
            event_type_pattern="bus.hearth.session.activity.v1",
            handler_fn="handle_session_activity",
            does_sse=True,
            notes="Bridges session activity to SSE",
        ),
        "handle_session_idle": HandlerSpec(
            event_type_pattern="bus.hearth.session.idle.v1",
            handler_fn="handle_session_idle",
            does_sse=True,
            notes="Bridges session idle to SSE",
        ),
        "handle_session_ended": HandlerSpec(
            event_type_pattern="bus.hearth.session.ended.v1",
            handler_fn="handle_session_ended",
            does_sse=True,
            notes="Bridges session ended to SSE",
        ),
        "handle_presence_update": HandlerSpec(
            event_type_pattern="hearth.presence.v1",
            handler_fn="handle_presence_update",
            notes="Updates in-memory presence state",
        ),
        "handle_tachyonos_event": HandlerSpec(
            event_type_pattern="tachyonos.",
            handler_fn="handle_tachyonos_event",
            does_db_write=True,
            does_sse=True,
            does_notification=True,
            does_research_dispatch=True,
            notes="Dispatches to sub-handlers: regime.changed→DB, market.signal→SSE, trade.proposed→notification",
        ),
        "handle_pattern_signal": HandlerSpec(
            event_type_pattern="bus.pattern.signal.v1",
            handler_fn="handle_pattern_signal",
            does_sse=True,
            notes="Logs pattern signals",
        ),
        "handle_kb_approved": HandlerSpec(
            event_type_pattern="hearth.kb.approved.v1",
            handler_fn="handle_kb_approved",
            does_file_write=True,
            notes="Writes KB entry from approved article",
        ),
    }

    return handler_traits


def build_flow_matrix(
    schemas: dict[str, SchemaSpec],
    handlers: dict[str, HandlerSpec]
) -> dict[str, dict[str, Any]]:
    """
    Build a matrix of schema → handler mapping.
    For each known event type, determine if hearth-api has a handler.
    """
    matrix = {}

    for schema_name, schema in schemas.items():
        # The schema name is the channel/event type
        event_type = schema.raw_event_type

        # Check if any handler matches this event type
        matching_handlers = []
        for handler_name, spec in handlers.items():
            pattern = spec.event_type_pattern
            if pattern.endswith(".*"):
                prefix = pattern[:-2]
                if event_type.startswith(prefix) or prefix in event_type:
                    matching_handlers.append(spec)
            elif event_type == pattern:
                matching_handlers.append(spec)

        handler = matching_handlers[0] if matching_handlers else None

        matrix[schema_name] = {
            "schema": schema,
            "handler": handler,
            "handler_name": handler.handler_fn if handler else None,
            "has_handler": handler is not None,
            "does_db_write": handler.does_db_write if handler else False,
            "does_sse": handler.does_sse if handler else False,
            "does_notification": handler.does_notification if handler else False,
            "does_file_write": handler.does_file_write if handler else False,
            "does_research_dispatch": handler.does_research_dispatch if handler else False,
            "notes": handler.notes if handler else "NO HANDLER — events persisted only via generic tap",
        }

    return matrix


def sample_nbus_flow(count: int = 100) -> dict[str, int]:
    """Sample nbus:all to get counts of event types flowing through."""
    event_counts: dict[str, int] = defaultdict(int)

    if redis is None:
        return event_counts

    try:
        client = redis.from_url(REDIS_URI)
        entries = client.xrange(REDIS_STREAM_NBUS, count=count)

        for _, fields in entries:
            raw = get_str(fields, "_raw")
            if raw:
                try:
                    payload = json.loads(raw)
                    et = payload.get("type", "unknown")
                    event_counts[et] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

        client.close()

    except redis.ConnectionError:
        pass
    except Exception as e:
        print(f"WARNING: error sampling nbus:all: {e}", file=sys.stderr)

    return dict(event_counts)


# ── Part 5: Tachyonos Deep Dive ──────────────────────────────────────────────

@dataclass
class TachyonosFlow:
    regime_changed_handler_working: bool = False
    market_signal_handler_working: bool = False
    trade_proposed_handler_working: bool = False
    temple_stuart_connected: bool = False
    db_has_recent_epochs: bool = False
    regime_events_24h: int = 0
    signal_events_24h: int = 0
    trade_proposed_events_24h: int = 0
    last_regime_event: str | None = None
    last_signal_event: str | None = None
    diagnosis: str = ""


def analyze_tachyonos_flow() -> TachyonosFlow:
    """
    Check tachyonos flow from bus → hearth-api → DB.
    1. Sample nbus:all for tachyonos events in last 24h
    2. Check DB for market_regime_epochs data
    3. Analyze what's missing
    """
    flow = TachyonosFlow()

    if redis is None:
        flow.diagnosis = "Redis not available — cannot analyze tachyonos flow"
        return flow

    try:
        client = redis.from_url(REDIS_URI)

        # Get last 100 entries to estimate tachyonos event volumes
        entries = client.xrange(REDIS_STREAM_NBUS, count=100)

        tachyonos_events = {"regime.changed": 0, "market.signal": 0, "trade.proposed": 0}
        last_events = {"regime.changed": None, "market.signal": None, "trade.proposed": None}

        for entry_id, fields in entries:
            event_type = None
            raw = get_str(fields, "_raw")
            if raw:
                try:
                    payload = json.loads(raw)
                    event_type = payload.get("type", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                event_type = get_str(fields, "type")

            if event_type and event_type.startswith("tachyonos."):
                if "regime.changed" in event_type:
                    tachyonos_events["regime.changed"] += 1
                    if last_events["regime.changed"] is None:
                        last_events["regime.changed"] = event_type
                elif "market.signal" in event_type:
                    tachyonos_events["market.signal"] += 1
                    if last_events["market.signal"] is None:
                        last_events["market.signal"] = event_type
                elif "trade.proposed" in event_type:
                    tachyonos_events["trade.proposed"] += 1
                    if last_events["trade.proposed"] is None:
                        last_events["trade.proposed"] = event_type

        flow.regime_events_24h = tachyonos_events["regime.changed"]
        flow.signal_events_24h = tachyonos_events["market.signal"]
        flow.trade_proposed_events_24h = tachyonos_events["trade.proposed"]
        flow.last_regime_event = last_events["regime.changed"]
        flow.last_signal_event = last_events["market.signal"]

        # Check if tachyonos handlers exist in hearth-api
        handlers = load_hearth_handlers()
        tachyonos_handler = handlers.get("handle_tachyonos_event")
        if tachyonos_handler:
            flow.regime_changed_handler_working = True
            flow.market_signal_handler_working = True

        # Try to query DB for market_regime_epochs
        try:
            import subprocess
            result = subprocess.run(
                ["psql", "-h", "192.168.1.45", "-U", "hearth", "-d", "hearth",
                 "-t", "-c", "SELECT COUNT(*) FROM market_regime_epochs LIMIT 1;"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "PGPASSWORD": os.environ.get("HEARTH_DB_PASS", "")}
            )
            if result.returncode == 0:
                count = int(result.stdout.strip())
                flow.db_has_recent_epochs = count > 0
        except Exception:
            pass  # DB not accessible

        # Build diagnosis
        issues = []
        if flow.regime_events_24h == 0:
            issues.append("No tachyonos.regime.changed events in last 100 nbus:all entries")
        if not flow.db_has_recent_epochs:
            issues.append("market_regime_epochs table is empty (temple_stuart not connected or no data)")

        if issues:
            flow.diagnosis = "; ".join(issues)
        else:
            if flow.regime_changed_handler_working and flow.db_has_recent_epochs:
                flow.temple_stuart_connected = True
                flow.diagnosis = "Tachyonos regime flow appears healthy"
            else:
                flow.diagnosis = "Tachyonos flow verified: handlers present, events flowing"

        client.close()

    except redis.ConnectionError as e:
        flow.diagnosis = f"Could not connect to Redis: {e}"
    except Exception as e:
        flow.diagnosis = f"Error analyzing tachyonos flow: {e}"

    return flow


# ── Schema Fix Generator ─────────────────────────────────────────────────────

def generate_updated_schema(schema_spec: SchemaSpec, groups: list[ViolationGroup]) -> dict:
    """
    Generate an updated schema by applying violation patches.
    Returns a dict ready to serialize as JSON.
    """
    schema_path = schema_spec.path
    with open(schema_path) as f:
        schema = json.load(f)

    for group in groups:
        if group.json_patch is None:
            continue

        patch = group.json_patch
        if patch["op"] == "add":
            # Navigate to the enum array and append
            path_parts = patch["path"].strip("/").split("/")
            current = schema

            # Navigate to parent of the target
            for part in path_parts[:-1]:
                if part.isdigit():
                    current = current[int(part)]
                else:
                    current = current.get(part, current)

            target = path_parts[-1]
            if target == "-" and isinstance(current, list):
                # JSON Patch "-" means append to array
                val = patch["value"]
                if val not in current:
                    current.append(val)
            elif target == "enum" and isinstance(current, dict):
                if "enum" not in current:
                    current["enum"] = []
                if isinstance(current["enum"], list):
                    val = patch["value"]
                    if val not in current["enum"]:
                        current["enum"].append(val)

    return schema


# ── Reporter ───────────────────────────────────────────────────────────────

def print_report(
    schemas: dict[str, SchemaSpec],
    dead_letters: list[DeadLetter],
    groups: list[ViolationGroup],
    flow_matrix: dict[str, dict],
    tachyonos_flow: TachyonosFlow,
    schema_event_counts: dict[str, int],
):
    """Print a comprehensive markdown report to stdout."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"# Schema Hygiene Report — {now}")
    print()

    # ── Part 1: Dead letter breakdown ────────────────────────────────────
    print("## Part 1 — Dead Letter Breakdown")
    print()
    print(f"Total dead letters analyzed: **{len(dead_letters)}**")
    print(f"Unique violation groups: **{len(groups)}**")
    print()

    if groups:
        print("| Failure Reason | Count | Event Type | Field | Proposed Fix | Confidence |")
        print("|---|---|---|---|---|---|---|")
        for g in groups:
            failure_type = g.violation_detail[:60] if g.violation_detail else g.failure_reason
            print(f"| {g.failure_reason} | {g.count} | `{g.original_event_type}` | {g.field_name or 'N/A'} | {g.proposed_fix[:80]} | {g.confidence.value} |")
    else:
        print("No dead letters found in `nbus:bus.dead_letter` stream.")
    print()

    # ── Part 2: Schema Coverage Matrix ──────────────────────────────────
    print("## Part 2 — Schema Coverage Matrix (hearth-api handlers)")
    print()
    print("| Schema / Event Type | Handler | DB Write | SSE | Notification | Research | Notes |")
    print("|---|---|---|---|---|---|---|---|")

    for schema_name, entry in sorted(flow_matrix.items(), key=lambda x: x[1]["handler_name"] or ""):
        handler_name = entry["handler_name"] or "*(none)*"
        db = "Y" if entry["does_db_write"] else "-"
        sse = "Y" if entry["does_sse"] else "-"
        notif = "Y" if entry["does_notification"] else "-"
        research = "Y" if entry["does_research_dispatch"] else "-"
        notes = entry["notes"][:60]
        has_events = "*" if schema_name in schema_event_counts else ""

        print(f"| {has_events}`{schema_name}` | {handler_name} | {db} | {sse} | {notif} | {research} | {notes} |")

    print()
    print(f"* = event type observed in `nbus:all` in sample of last ~100 entries")
    print()

    # ── Part 3: Tachyonos Flow Analysis ─────────────────────────────────
    print("## Part 3 — Tachyonos Flow Analysis")
    print()
    print(f"Analyzed: `nbus:all` stream (~last 100 entries) + `nbus:bus.dead_letter`")
    print()
    print(f"| Metric | Value |")
    print("|---|---|")
    print(f"| `tachyonos.regime.changed` events in sample | {tachyonos_flow.regime_events_24h} |")
    print(f"| `tachyonos.market.signal` events in sample | {tachyonos_flow.signal_events_24h} |")
    print(f"| `tachyonos.trade.proposed` events in sample | {tachyonos_flow.trade_proposed_events_24h} |")
    print(f"| Last regime event | {tachyonos_flow.last_regime_event or 'none seen'} |")
    print(f"| Regime handler status | {'working' if tachyonos_flow.regime_changed_handler_working else 'missing'} |")
    print(f"| temple_stuart connected | {'yes' if tachyonos_flow.temple_stuart_connected else 'no / unknown'} |")
    print(f"| market_regime_epochs has data | {'yes' if tachyonos_flow.db_has_recent_epochs else 'empty'} |")
    print()
    print(f"**Diagnosis:** {tachyonos_flow.diagnosis}")
    print()

    # ── Part 4: Recommended Schema Updates ──────────────────────────────
    actionable = [g for g in groups if g.json_patch is not None and g.confidence in [Confidence.HIGH, Confidence.MEDIUM]]
    if actionable:
        print("## Part 4 — Recommended Schema Updates")
        print()
        print("### Exact JSON Patches")
        print()
        for g in actionable:
            print(f"**Schema:** `{g.original_event_type}`")
            print(f"**Field:** `{g.field_name}`")
            print(f"**Current enum:** `[{', '.join(repr(v) for v in g.current_enum_values)}]`")
            print(f"**Add:** `[{', '.join(repr(v) for v in g.failed_values)}]`")
            print(f"**Confidence:** {g.confidence.value}")
            print()
            print("```json")
            patch_display = {
                "op": g.json_patch["op"],
                "path": g.json_patch["path"],
                "value": g.json_patch["value"]
            }
            print(json.dumps(patch_display, indent=2))
            print("```")
            print()

    # ── Part 5: Allowlist Violations (not schema issues) ─────────────────
    allowlist = [g for g in groups if g.failure_reason == "allowlist_violation"]
    if allowlist:
        print("## Part 5 — Allowlist Violations (Not Schema Issues)")
        print()
        for g in allowlist:
            print(f"- Channel `{g.channel}`: {g.count} occurrences — {g.diagnosis}")
        print()
        print("These need to be added to `nbus-publish --allowlist`, not schema changes.")
        print()


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="nervous-bus schema hygiene tool")
    parser.add_argument("--report", action="store_true", help="Print comprehensive markdown report")
    parser.add_argument("--fix", action="store_true", help="Write updated schema files (*_v2.json)")
    parser.add_argument("--dry-run", action="store_true", help="With --fix, only print what would change, don't write files")
    args = parser.parse_args()

    if not args.report and not args.fix:
        parser.print_help()
        return

    print("Loading schemas...", file=sys.stderr)
    schemas = load_schemas()
    print(f"Loaded {len(schemas)} schemas", file=sys.stderr)

    print("Reading dead letters from Redis...", file=sys.stderr)
    dead_letters = read_dead_letters()
    print(f"Read {len(dead_letters)} dead letters", file=sys.stderr)

    print("Loading hearth-api handlers...", file=sys.stderr)
    handlers = load_hearth_handlers()
    print(f"Loaded {len(handlers)} handler specs", file=sys.stderr)

    print("Building flow matrix...", file=sys.stderr)
    flow_matrix = build_flow_matrix(schemas, handlers)

    print("Sampling nbus:all for live event counts...", file=sys.stderr)
    schema_event_counts = sample_nbus_flow()

    print("Running tachyonos deep dive...", file=sys.stderr)
    tachyonos_flow = analyze_tachyonos_flow()

    print("Grouping dead letters and hypothesizing gaps...", file=sys.stderr)
    groups_dict = group_dead_letters(dead_letters)
    violation_groups = hypothesize_gaps(dict(groups_dict), schemas)

    if args.report:
        print_report(schemas, dead_letters, violation_groups, flow_matrix, tachyonos_flow, schema_event_counts)

    if args.fix:
        actionable = [g for g in violation_groups
                     if g.json_patch is not None
                     and g.confidence in [Confidence.HIGH, Confidence.MEDIUM]]

        if not actionable:
            print("No actionable schema fixes found.", file=sys.stderr)
            return

        print(f"Found {len(actionable)} actionable fix(s)", file=sys.stderr)

        if args.dry_run:
            print("DRY RUN — would apply the following patches:", file=sys.stderr)
            for g in actionable:
                print(f"  {g.original_event_type}: {g.json_patch}", file=sys.stderr)
        else:
            # Deduplicate by schema — apply all patches for same schema together
            from collections import defaultdict
            by_schema: dict[str, list[ViolationGroup]] = defaultdict(list)
            for g in actionable:
                by_schema[g.original_event_type].append(g)

            for schema_name, groups_for_schema in by_schema.items():
                if schema_name not in schemas:
                    print(f"WARNING: no schema found for {schema_name}", file=sys.stderr)
                    continue

                schema_spec = schemas[schema_name]
                updated = generate_updated_schema(schema_spec, groups_for_schema)

                out_name = schema_spec.path.stem + "_v2.json"
                out_path = SCHEMAS_DIR / out_name
                with open(out_path, "w") as f:
                    json.dump(updated, f, indent=2)

                n_patches = len(groups_for_schema)
                print(f"Wrote {out_path} ({n_patches} patch(es))", file=sys.stderr)


if __name__ == "__main__":
    main()
