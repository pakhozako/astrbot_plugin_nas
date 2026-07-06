"""Command text parsing helpers for AstrBot handlers."""

import shlex


def parse_args(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=False)
    except ValueError:
        return text.strip().split()


def strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def message_text(event) -> str:
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            pass
    return str(getattr(event, "message_str", "") or "").strip()


def command_payload(event, commands: set[str]) -> str:
    text = message_text(event)
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    head = parts[0].lstrip("/")
    if head in commands:
        return parts[1].strip() if len(parts) > 1 else ""
    return text


def split_command_args(event, commands: set[str], maxsplit: int = -1) -> list[str]:
    payload = command_payload(event, commands)
    return payload.split(maxsplit=maxsplit) if payload else []


def parse_command_args(event, commands: set[str]) -> list[str]:
    return parse_args(command_payload(event, commands))


def split_first_command_arg(event, commands: set[str], keep_unquoted: bool = False) -> list[str]:
    payload = command_payload(event, commands).strip()
    if not payload:
        return []
    if payload[0] in {"'", '"'}:
        quote = payload[0]
        end = payload.find(quote, 1)
        if end > 0:
            first = payload[1:end]
            rest = payload[end + 1 :].strip()
            return [first, rest] if rest else [first]
    if keep_unquoted:
        return [payload]
    return payload.split(maxsplit=1)
