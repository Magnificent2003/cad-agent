"""Detect completion of a new turn without confusing it with previous replies."""
from typing import Any


def terminal_assistant(messages: list[dict[str, Any]], previous_ids: set[str]) -> dict[str, Any] | None:
    current = [
        item for item in messages
        if item.get("info", {}).get("role") == "assistant"
        and item.get("info", {}).get("id") not in previous_ids
    ]
    if not current:
        return None
    message = current[-1]
    info = message["info"]
    if info.get("error") or (
        info.get("time", {}).get("completed")
        and info.get("finish") not in {None, "tool-calls", "unknown"}
    ):
        return message
    return None

