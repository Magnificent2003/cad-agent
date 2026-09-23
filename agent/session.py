"""Separate internal engine messages, stopped model turns and usable replies."""
from typing import Any


def is_internal_part(part: dict[str, Any]) -> bool:
    metadata = part.get("metadata") or {}
    return bool(
        part.get("synthetic") or part.get("ignored") or part.get("type") == "compaction"
        or metadata.get("compaction_continue") or metadata.get("internal")
    )


def is_internal_message(message: dict[str, Any]) -> bool:
    info = message.get("info", message)
    if (
        info.get("role") not in {"user", "assistant"}
        or info.get("summary") or info.get("synthetic") or info.get("internal")
        or info.get("agent") in {"compaction", "title", "summary"}
        or info.get("mode") in {"compaction", "title", "summary"}
    ):
        return True
    parts = message.get("parts", [])
    return bool(info.get("role") == "user" and parts and all(is_internal_part(part) for part in parts))


def has_visible_text(message: dict[str, Any]) -> bool:
    return not is_internal_message(message) and any(
        part.get("type") == "text" and not is_internal_part(part) and part.get("text", "").strip()
        for part in message.get("parts", [])
    )


def needs_continuation(message: dict[str, Any]) -> bool:
    """A stopped model can still owe the user a reply; errors are not retried here."""
    info = message.get("info", message)
    return bool(
        info.get("role") == "assistant" and not is_internal_message(message) and not info.get("error")
        and info.get("time", {}).get("completed")
        and (info.get("finish") == "length" or (
            info.get("finish") == "stop" and not has_visible_text(message)
        ))
    )


def terminal_assistant(messages: list[dict[str, Any]], previous_ids: set[str]) -> dict[str, Any] | None:
    current = [
        item for item in messages
        if item.get("info", {}).get("role") == "assistant"
        and item.get("info", {}).get("id") not in previous_ids
        and not is_internal_message(item)
    ]
    if not current:
        return None
    # Imported histories may not be ordered; ties retain the received order.
    message = max(enumerate(current), key=lambda item: (
        item[1]["info"].get("time", {}).get("created", 0), item[0]
    ))[1]
    info = message["info"]
    if info.get("error") or (
        info.get("time", {}).get("completed")
        and info.get("finish") not in {None, "tool-calls", "unknown"}
    ):
        return message
    return None
