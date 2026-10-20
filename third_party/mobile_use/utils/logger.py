# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

from enum import Enum
import logging
import os
from pathlib import Path
import queue
import sys
import threading

from colorama import Fore, Style, init

from artemis.data_engine.context_vars import CURRENT_TRACE_ID
from artemis.runtime.execution_scope import (
    CURRENT_EXECUTION,
    execution_scope,
    get_current_data_engine,
)

init(autoreset=True)


class LogLevel(Enum):
    DEBUG = ("DEBUG", Fore.MAGENTA, "🔍")
    INFO = ("INFO", Fore.WHITE, "ℹ")
    SUCCESS = ("SUCCESS", Fore.GREEN, "✓")
    WARNING = ("WARNING", Fore.YELLOW, "⚠")
    ERROR = ("ERROR", Fore.RED, "❌")
    CRITICAL = ("CRITICAL", Fore.RED + Style.BRIGHT, "💥")


def record_log_entry(item):
    """Persist using ownership captured by emit, never the consumer's globals."""
    engine, scope, step_id, record_name, message, level_name, formatted, parent_id = item
    if engine and engine.current_session_id:
        from contextlib import nullcontext

        with execution_scope(scope) if scope is not None else nullcontext():
            engine.record_trace(
                type="log",
                name=record_name,
                payload={"message": message, "level": level_name, "formatted": formatted},
                status="success",
                parent_trace_id=parent_id,
                step_id=step_id,
            )


class DataEngineHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_queue = queue.Queue(maxsize=5000)
        self._write_errors = []
        self._error_lock = threading.Lock()
        self._worker_thread = threading.Thread(target=self._drain_queue, daemon=True)
        self._worker_thread.start()

    def _drain_queue(self):
        while True:
            item = self._log_queue.get()
            if item is None:
                break
            if isinstance(item, threading.Event):
                item.set()
                self._log_queue.task_done()
                continue
            engine = item[0]
            try:
                record_log_entry(item)
            except Exception as exc:
                # Keep draining: a dead consumer would make every flush hang.
                with self._error_lock:
                    self._write_errors.append((engine, exc))
            finally:
                self._log_queue.task_done()

    def emit(self, record):
        _CURRENT_DATA_ENGINE = get_current_data_engine()

        if (
            _CURRENT_DATA_ENGINE
            and getattr(_CURRENT_DATA_ENGINE, "current_session_id", None)
            and callable(getattr(_CURRENT_DATA_ENGINE, "record_trace", None))
        ):
            log_entry = self.format(record)
            log_level = getattr(record, "log_level", None)
            if log_level and hasattr(log_level, "value"):
                level_name, _color, _symbol = log_level.value
            else:
                level_name = record.levelname

            parent_id = CURRENT_TRACE_ID.get()
            entry = (
                _CURRENT_DATA_ENGINE,
                CURRENT_EXECUTION.get(),
                getattr(_CURRENT_DATA_ENGINE, "current_step_id", None),
                record.name,
                record.getMessage(),
                level_name,
                log_entry,
                parent_id,
            )
            enqueue = getattr(_CURRENT_DATA_ENGINE, "enqueue_log", None)
            if callable(enqueue):
                enqueue(entry)
                return
            try:
                self._log_queue.put_nowait(entry)
            except queue.Full as e:
                raise RuntimeError(
                    "DataEngine log queue is full - cannot accept more log"
                    " entries without dropping records"
                ) from e


async def flush_data_engine_logs(engine):
    """Place FIFO barriers after producers stop, then surface persistence errors."""
    import asyncio

    flush = getattr(engine, "flush_logs", None)
    if callable(flush):
        await flush()
        return

    handlers = {
        handler
        for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
        for handler in value.handlers
        if isinstance(handler, DataEngineHandler)
    }
    for handler in handlers:
        barrier = threading.Event()
        await asyncio.to_thread(handler._log_queue.put, barrier)
        await asyncio.to_thread(barrier.wait)
        with handler._error_lock:
            errors = [error for owner, error in handler._write_errors if owner is engine]
            handler._write_errors = [
                (owner, error) for owner, error in handler._write_errors if owner is not engine
            ]
        if errors:
            # Surface each failure once. The owning Role stays quarantined,
            # but a later close must still be able to drain and release it.
            raise RuntimeError("DataEngine log persistence failed") from errors[0]


class ArtemisLogger:
    def __init__(
        self,
        name: str,
        console_level: str = "INFO",
    ):
        """Initialize the ARTEMIS logger.

        Args:
            name: Logger name (usually __name__)
            console_level: Minimum level for console output
        """
        self.name = name
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        self.logger.handlers.clear()
        self.logger.propagate = False

        self._setup_console_handler(console_level)

        # Add DataEngineHandler
        data_engine_handler = DataEngineHandler()
        data_engine_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(fmt="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
        data_engine_handler.setFormatter(formatter)
        self.logger.addHandler(data_engine_handler)

        if os.environ.get("ARTEMIS_MCP_SERVER") == "true":
            traces_dir = os.environ.get("ARTEMIS_TRACES_PATH", "traces")
            log_path = Path(traces_dir) / "mcp_server.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    def _setup_console_handler(self, level: str):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(getattr(logging, level.upper()))

        console_formatter = ColoredFormatter()
        console_handler.setFormatter(console_formatter)

        self.logger.addHandler(console_handler)

    def debug(self, message: str, **kwargs):
        self.logger.debug(message, extra={"log_level": LogLevel.DEBUG}, **kwargs)

    def info(self, message: str, **kwargs):
        self.logger.info(message, extra={"log_level": LogLevel.INFO}, **kwargs)

    def success(self, message: str, **kwargs):
        self.logger.info(message, extra={"log_level": LogLevel.SUCCESS}, **kwargs)

    def warning(self, message: str, **kwargs):
        self.logger.warning(message, extra={"log_level": LogLevel.WARNING}, **kwargs)

    def error(self, message: str, **kwargs):
        self.logger.error(message, extra={"log_level": LogLevel.ERROR}, **kwargs)

    def critical(self, message: str, **kwargs):
        self.logger.critical(message, extra={"log_level": LogLevel.CRITICAL}, **kwargs)

    def header(self, message: str, **_kwargs):
        separator = "=" * 60
        colored_separator = f"{Fore.CYAN}{separator}{Style.RESET_ALL}"
        colored_message = f"{Fore.CYAN}{message}{Style.RESET_ALL}"

        print(colored_separator, file=sys.stderr)
        print(colored_message, file=sys.stderr)
        print(colored_separator, file=sys.stderr)
        self.logger.info(f"\n{separator}\n{message}\n{separator}")


class ColoredFormatter(logging.Formatter):
    def format(self, record):
        log_level = getattr(record, "log_level", LogLevel.INFO)
        _level_name, color, symbol = log_level.value

        colored_message = f"{color}{symbol} {record.getMessage()}{Style.RESET_ALL}"

        return colored_message


_loggers = {}


def get_logger(
    name: str,
    console_level: str = "INFO",
) -> ArtemisLogger:
    """Get or create a logger instance.

    Args:
        name: Logger name (usually __name__)
        log_file: Path to log file (defaults to logs/{name}.log)
        console_level: Minimum level for console output
        file_level: Minimum level for file output
        enable_file_logging: Whether to enable file logging

    Returns:
        ArtemisLogger instance
    """
    if name not in _loggers:
        _loggers[name] = ArtemisLogger(
            name=name,
            console_level=console_level,
        )

    return _loggers[name]
