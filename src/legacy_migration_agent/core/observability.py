"""Safe, opt-in terminal lifecycle logging for the interactive application.

Lifecycle events contain controller-owned identifiers and status codes only.
They intentionally exclude prompts, model content, generated source, command
output, filesystem paths, credentials, and exception messages.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, TextIO, TypeAlias

LifecycleValue: TypeAlias = str | int | float | bool | None

_LOGGER_NAME: Final = "legacy_migration_agent.lifecycle"
_TOKEN: Final = re.compile(r"^[a-z][a-z0-9_.]{0,79}$")
_FIELD_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_STRING_LENGTH: Final = 500
_LOGGER = logging.getLogger(_LOGGER_NAME)
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False


def lifecycle_event(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: LifecycleValue,
) -> None:
    """Emit one injection-safe lifecycle event without affecting execution."""

    safe_event = event if _TOKEN.fullmatch(event) is not None else "observability.invalid_event"
    parts = [f"event={safe_event}"]
    for name, value in fields.items():
        if _FIELD_NAME.fullmatch(name) is None:
            continue
        parts.append(f"{name}={_encoded_value(value)}")
    _LOGGER.log(level, " ".join(parts))


@contextmanager
def terminal_lifecycle_logging(*, stream: TextIO | None = None) -> Iterator[None]:
    """Route lifecycle events to stderr for the duration of one UI server."""

    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    previous_level = _LOGGER.level
    previous_propagation = _LOGGER.propagate
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    try:
        yield
    finally:
        _LOGGER.removeHandler(handler)
        handler.close()
        _LOGGER.setLevel(previous_level)
        _LOGGER.propagate = previous_propagation


def _encoded_value(value: object) -> str:
    if isinstance(value, str):
        normalized: str | int | float | bool | None = value[:_MAX_STRING_LENGTH]
    elif isinstance(value, float) and not math.isfinite(value):
        normalized = "non_finite"
    elif value is None or isinstance(value, (bool, int, float)):
        normalized = value
    else:  # pragma: no cover - defensive against untyped callers
        normalized = "invalid_type"
    return json.dumps(normalized, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
