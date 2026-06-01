#!/usr/bin/env bash
# format-tail.sh — reads the newest nervous-bus event and emits one zjstatus line.
#
# Use as a zjstatus command_nervous:
#   format_left = "#[fg=cyan]{command_nervous_output}"
#   command_nervous = "/path/to/format-tail.sh"
#   command_nervous_interval = 5
#   command_nervous_click_handler = ""

set -euo pipefail

LOG="${NERVOUS_DEBUG_LOG:-$HOME/.cache/nervous-bus/debug.jsonl}"
IDLE_AFTER=60  # seconds before showing "idle"

if [[ ! -f "$LOG" ]]; then
    printf 'bus: no log'
    exit 0
fi

# Read the last line of the log
last="$(tail -n 1 "$LOG" 2>/dev/null)"
if [[ -z "$last" ]]; then
    printf 'bus: idle'
    exit 0
fi

# Parse the event time to check staleness
event_time="$(printf '%s' "$last" | jq -r '.time // empty' 2>/dev/null)"
if [[ -n "$event_time" ]]; then
    now=$(date -u +%s)
    event_epoch=$(date -u -d "$event_time" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$event_time" +%s 2>/dev/null || echo 0)
    age=$(( now - event_epoch ))
    if (( age > IDLE_AFTER )); then
        printf 'bus: idle'
        exit 0
    fi
fi

# Extract key fields
channel="$(printf '%s' "$last" | jq -r '.type // "unknown"' 2>/dev/null)"
source="$(printf '%s' "$last" | jq -r '.source // ""' 2>/dev/null | sed 's|^/||')"

# Project-specific summary: pick the most useful data field
summary="$(printf '%s' "$last" | jq -r '
    .data |
    if type == "object" then
        if .fps      then "fps=\(.fps)"
        elif .bead_id then "bead=\(.bead_id)"
        elif .status  then .status
        elif .seq     then "seq=\(.seq)"
        elif .i       then "i=\(.i)"
        else to_entries | map("\(.key)=\(.value)") | first // ""
        end
    else ""
    end
' 2>/dev/null)"

line="${source} ${channel}"
if [[ -n "$summary" ]]; then
    line="${line} ${summary}"
fi

# Truncate to 80 chars
printf '%.80s' "bus: $line"
