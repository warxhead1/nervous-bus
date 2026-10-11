"""Producers that share bus.agent.activity.v1 but are not LLM agents.

tachyonac-engine publishes every cron/ingestor job tick on the agent-activity
channel (since 2026-10-03, ~34k two-event "runs"/day). Recorded as runs they
drowned label.py and synthesis.py past their nightly timeouts, so the recorder
drops them and retire_non_agent_runs.py moves the backlog out of runs.db.
"""
NON_AGENT_KINDS = frozenset({"cron", "ingestor"})
