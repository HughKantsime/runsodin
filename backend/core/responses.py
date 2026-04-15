"""Response envelope helpers for agent-friendly endpoints.

v1.8.9 agent-surface convention: every write endpoint returns a
`next_actions: list[dict]` field that hints the next tool call(s) an
agent might reasonably make. This is a *soft hint* — no enforcement
client-side, no schema constraint server-side. It exists to let a
weak local model (Qwen2.5-14B, etc.) make the right next call
without prompt-engineering heroics.

Shape:
    next_actions: [
        {"tool": "get_job",     "args": {"id": 42}, "reason": "check status"},
        {"tool": "list_queue",  "args": {},         "reason": "see queue depth"},
    ]

Fields are all optional except `tool`. Keep `reason` short — agents
log these as trace data and long strings bloat transcripts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def next_action(
    tool: str,
    args: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a single next-action suggestion dict."""
    entry: Dict[str, Any] = {"tool": tool}
    if args is not None:
        entry["args"] = args
    if reason:
        entry["reason"] = reason
    return entry


def build_next_actions(*entries: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compose a next_actions list from individual entries.

    Convenience wrapper so callers can write:
        return {
            ...,
            "next_actions": build_next_actions(
                next_action("get_job", {"id": job.id}, "check status"),
                next_action("list_queue", reason="see queue depth"),
            ),
        }
    """
    return [e for e in entries if e]
