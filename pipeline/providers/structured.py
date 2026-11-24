"""Turn raw model text into a validated schema object, with bounded retries.

Model output is untrusted data. It is parsed, never executed, and never logged or kept in an
error: a failure records only the error class name.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from pipeline.providers.base import M, ModelSpec, StructuredOutputFailed

__all__ = ["error_shape", "extract_json", "structured_from_text"]

_KNOWN_KEYS = frozenset(
    {"setting", "action", "time_of_day", "weather", "shot_hint", "figures"}
    | {"description", "emotion", "value", "evidence"}
)
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


def extract_json(text: str) -> str:
    """The first balanced top-level JSON object in `text`.

    One leading <think>...</think> block is skipped (an unclosed one is an error: the model was
    cut off while thinking). Code fences and prose around the object are ignored. Raises
    ValueError when there is no complete object.
    """
    body = text.lstrip()
    if body.startswith(_THINK_OPEN):
        end = body.find(_THINK_CLOSE)
        if end < 0:
            raise ValueError("unterminated think block")
        body = body[end + len(_THINK_CLOSE) :]
    start = body.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(body)):
        c = body[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return body[start : i + 1]
    raise ValueError("unbalanced JSON object")


def error_shape(exc: Exception) -> str:
    """Field paths and error types of a failed validation, safe to log.

    Only schema field names and list indexes appear in a path; any other key came from the model
    and is shown as "?". Values are never included. Other errors give the class name only.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return type(exc).__name__
    parts: list[str] = []
    for e in errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) if isinstance(p, int) or p in _KNOWN_KEYS else "?" for p in e["loc"])
        parts.append(f"{loc or '<root>'}: {e['type']}")
    return "; ".join(parts)


def structured_from_text(
    spec: ModelSpec,
    schema: type[M],
    generate: Callable[[int], str],
    max_attempts: int,
    on_invalid: Callable[[int, str], None] | None = None,
) -> M:
    """Call `generate(attempt)` (1-based) until its text validates as `schema`.

    Exactly `max_attempts` calls at most. Then StructuredOutputFailed with the last error's
    class name only (never its message, which may quote the output).
    """
    last = "ValueError"
    for attempt in range(1, max_attempts + 1):
        raw = generate(attempt)
        try:
            return schema.model_validate_json(extract_json(raw))
        except (ValueError, json.JSONDecodeError) as e:  # ValidationError is a ValueError
            last = type(e).__name__
            if on_invalid is not None:
                on_invalid(attempt, error_shape(e))
    raise StructuredOutputFailed(spec, max_attempts, last) from None
