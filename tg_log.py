"""
HexClaw — tg_log.py
====================
Centralized Telegram logging handler.

Captures every log event across all HexClaw modules and forwards them to the
operator's Telegram chatroom — giving full visibility and control from a single
pane of glass.

Features:
  • Batches messages (default 3 s) to avoid Telegram rate-limits (30 msg/s)
  • Severity-based emoji prefixes for quick visual scanning
  • Filters noisy low-level logs (httpx, urllib3) by default
  • Thread-safe queue; non-blocking — never stalls the daemon
  • Captures "thought process" breadcrumbs from inference + planner modules

Usage (typically called once in daemon.py):
    import tg_log
    tg_log.install()
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("hexclaw.tg_log")

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: int = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Minimum severity forwarded to Telegram (DEBUG, INFO, WARNING, ERROR, CRITICAL)
TG_LOG_LEVEL: str = os.getenv("TG_LOG_LEVEL", "INFO").upper()

# Seconds to batch messages before sending (avoids rate-limit)
BATCH_INTERVAL: float = float(os.getenv("TG_LOG_BATCH_SEC", "3"))

# Maximum message length (Telegram hard limit is 4096)
MAX_MSG_LEN: int = 4000

# Modules whose logs are silenced in Telegram (still go to file/console)
MUTED_LOGGERS: set[str] = {
    "httpx",
    "urllib3",
    "httpcore",
    "asyncio",
    "telegram.ext._updater",
    "telegram.ext._application",
    "telegram._bot",
    "hpack",
    "h11",
}

# ── Emoji map ─────────────────────────────────────────────────────────────────

_EMOJI: dict[int, str] = {
    logging.DEBUG:    "🔍",
    logging.INFO:     "ℹ️",
    logging.WARNING:  "⚠️",
    logging.ERROR:    "❌",
    logging.CRITICAL: "🔴",
}

# Special prefixes for "thought process" logs (inference, planner, monitor)
_THOUGHT_MODULES: dict[str, str] = {
    "hexclaw.inference": "🧠",
    "hexclaw.planner":   "🗺️",
    "hexclaw.monitor":   "📡",
    "hexclaw.cache":     "💾",
    "hexclaw.data":      "📊",
    "hexclaw.daemon":    "⚙️",
    "hexclaw.telegram":  "📱",
}


# ── Telegram Log Handler ─────────────────────────────────────────────────────

class TelegramLogHandler(logging.Handler):
    """
    Async-safe logging handler that batches log records and sends them
    to a Telegram chat via a background thread.

    Records are queued immediately (non-blocking) and flushed every
    BATCH_INTERVAL seconds by a daemon thread.
    """

    def __init__(self, token: str, chat_id: int, level: int = logging.INFO):
        super().__init__(level)
        self._token = token
        self._chat_id = chat_id
        self._queue: queue.Queue[str] = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bot: Any = None  # lazy-init to avoid import-time side effects

    # ── Handler interface ─────────────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        # Skip muted loggers
        if record.name in MUTED_LOGGERS or any(record.name.startswith(m) for m in MUTED_LOGGERS):
            return

        try:
            import html as _html
            emoji = _THOUGHT_MODULES.get(record.name, _EMOJI.get(record.levelno, "\U0001f4dd"))
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
            module_short = record.name.replace("hexclaw.", "")
            safe_msg = _html.escape(record.getMessage())

            msg = f"{emoji} <code>{ts}</code> <b>{module_short}</b> · {safe_msg}"

            # Append exception info if present
            if record.exc_info and record.exc_info[1]:
                safe_exc = _html.escape(str(record.exc_info[1]))
                msg += f"\n<pre>{safe_exc}</pre>"

            self._queue.put_nowait(msg)
        except queue.Full:
            pass  # Drop oldest silently — better than blocking the daemon
        except Exception:
            self.handleError(record)

    # ── Background sender thread ──────────────────────────────────────────

    def start(self) -> None:
        """Start the background flush thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="tg-log-flush")
        self._thread.start()
        log.debug("Telegram log handler started.")

    def stop(self) -> None:
        """Signal the flush thread to drain and exit."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _flush_loop(self) -> None:
        """Runs in a background thread: batch-sends queued messages."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self._stop.is_set():
            time.sleep(BATCH_INTERVAL)
            self._drain(loop)

        # Final drain on shutdown
        self._drain(loop)
        loop.close()

    def _drain(self, loop: asyncio.AbstractEventLoop) -> None:
        """Collect all queued messages and send as one batched Telegram message."""
        lines: list[str] = []
        while not self._queue.empty():
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if not lines:
            return

        # Build batched message, respecting Telegram's 4096-char limit
        batches = self._batch_lines(lines)
        for batch in batches:
            try:
                loop.run_until_complete(self._send(batch))
            except Exception as exc:
                # Print to stderr only — never re-log (infinite loop)
                import sys
                print(f"[tg_log] Send failed: {exc}", file=sys.stderr)

    @staticmethod
    def _batch_lines(lines: list[str]) -> list[str]:
        """Split lines into messages that fit within MAX_MSG_LEN."""
        batches: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1  # +1 for newline
            if current_len + line_len > MAX_MSG_LEN and current:
                batches.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len

        if current:
            batches.append("\n".join(current))

        return batches

    async def _send(self, text: str) -> None:
        """Send a single message via the Telegram Bot API."""
        if not self._bot:
            try:
                from telegram import Bot
                from telegram.request import HTTPXRequest
                # Dedicated pool so this thread never competes with the Application pool
                _req = HTTPXRequest(connection_pool_size=4, pool_timeout=20.0)
                self._bot = Bot(token=self._token, request=_req)
            except ImportError:
                return

        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text[:4096],
                parse_mode="HTML",
            )
        except Exception:
            # Fallback: try without HTML if formatting breaks
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text[:4096],
                )
            except Exception:
                pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_handler: TelegramLogHandler | None = None


def install(level: str | None = None) -> TelegramLogHandler | None:
    """
    Install the Telegram log handler on the root logger.

    Call once at daemon startup.  Safe to call multiple times (idempotent).
    Returns None if Telegram is not configured.
    """
    global _handler
    if _handler is not None:
        return _handler

    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram logging disabled — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return None

    effective_level = getattr(logging, (level or TG_LOG_LEVEL).upper(), logging.INFO)

    _handler = TelegramLogHandler(BOT_TOKEN, CHAT_ID, level=effective_level)
    _handler.start()

    # Attach to root logger so ALL modules are captured
    root = logging.getLogger()
    root.addHandler(_handler)

    # Use ASCII-safe message here — this log.info() goes to the console/stream
    # handler *before* anything ensures UTF-8 encoding, causing UnicodeEncodeError
    # on Windows CP1252 terminals when emoji characters are present.
    log.info("[tg_log] Telegram log handler installed (level=%s, batch=%ss)",
             logging.getLevelName(effective_level), BATCH_INTERVAL)

    return _handler


def uninstall() -> None:
    """Remove the handler and stop the background thread."""
    global _handler
    if _handler:
        logging.getLogger().removeHandler(_handler)
        _handler.stop()
        _handler = None
