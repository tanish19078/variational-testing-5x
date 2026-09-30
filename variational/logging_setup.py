"""Loguru configuration: console plus an optional rotating file sink.

Kept in one place so every entry point (bot, monitor, CLI) logs identically and
a run can be reconstructed afterwards from the file rather than from whatever
happened to still be in the terminal scrollback.

Secrets are scrubbed on the way out. The cookie and the connected address are
the two values that would otherwise end up in a log file that someone later
pastes into a bug report.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# Anything that looks like a session cookie or a wallet address.
_SCRUB_PATTERNS = [
    (re.compile(r"(vr-token=)[^;\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(cf_clearance=)[^;\s\"']+"), r"\1<redacted>"),
    (re.compile(r"\b(0x[a-fA-F0-9]{4})[a-fA-F0-9]{32}([a-fA-F0-9]{4})\b"), r"\1...\2"),
    # Bare JWTs (three base64url segments) wherever they appear.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
        "<jwt-redacted>",
    ),
]

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)


def scrub(text: str) -> str:
    """Redact credentials from a string. Exposed for use in tests."""
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub_patcher(record: dict[str, Any]) -> None:
    """Loguru patcher: rewrite the formatted message in place."""
    record["message"] = scrub(str(record["message"]))


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    *,
    rotation: str = "50 MB",
    retention: int = 3,
    backtrace: bool = False,
) -> None:
    """Install the console sink and, if ``log_file`` is set, a rotating file.

    Safe to call more than once: the existing sinks are removed first, so a
    second call reconfigures rather than duplicating every line.
    """
    logger.remove()
    logger.configure(patcher=_scrub_patcher)

    logger.add(
        sys.stderr,
        level=level.upper(),
        format=_CONSOLE_FORMAT,
        backtrace=backtrace,
        diagnose=False,  # never dump local variables: they hold the cookie
    )

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(path),
            level=level.upper(),
            format=_FILE_FORMAT,
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            enqueue=True,  # the bot is async; keep writes off the event loop
            backtrace=backtrace,
            diagnose=False,
        )
        logger.debug("logging to {} (rotation {}, keeping {})", path, rotation, retention)


def setup_from_config(config: Any) -> None:
    """Convenience wrapper for a :class:`config.Config`."""
    setup_logging(
        level=getattr(config, "log_level", "INFO") or "INFO",
        log_file=getattr(config, "log_file", "") or None,
    )
