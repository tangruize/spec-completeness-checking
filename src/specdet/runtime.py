"""Whole-analysis deadlines and cleanup of processes started by this tool."""
from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class RunDeadlineExceeded(BaseException):
    """Cancellation must not be mistaken for a recoverable backend error."""


_expires: ContextVar[float | None] = ContextVar("specdet_run_deadline", default=None)


def check_deadline() -> None:
    end = _expires.get()
    if end is not None and time.monotonic() >= end:
        raise RunDeadlineExceeded


def bounded_timeout(seconds: float) -> float:
    end = _expires.get()
    if end is None:
        return seconds
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise RunDeadlineExceeded
    return min(seconds, remaining)


def bounded_timeout_ms(milliseconds: int) -> int:
    return min(milliseconds, max(1, math.ceil(bounded_timeout(milliseconds / 1000) * 1000)))


@contextmanager
def run_deadline(seconds: float, *, started: float) -> Iterator[None]:
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        raise ValueError("Whole-run deadlines require a POSIX main thread; invoke the specdet CLI from other hosts/threads")
    if signal.getitimer(signal.ITIMER_REAL)[0] or _expires.get() is not None:
        raise ValueError("Cannot replace an existing process alarm or nest whole-run deadlines")
    previous = signal.getsignal(signal.SIGALRM)
    token = _expires.set(started + seconds)

    def expired(_signum, _frame):
        raise RunDeadlineExceeded

    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, bounded_timeout(seconds))
        yield
        check_deadline()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        _expires.reset(token)


def kill_owned_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


@contextmanager
def owned_process(
    argv: Sequence[str], *, cwd: Path, environment: Mapping[str, str],
    with_input: bool = False,
) -> Iterator[subprocess.Popen[str]]:
    process = None
    try:
        # Defer the alarm until the new process has an owner that can reap it.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM}) if _expires.get() is not None else None
        try:
            process = subprocess.Popen(
                list(argv), cwd=cwd, env=dict(environment), shell=False,
                stdin=subprocess.PIPE if with_input else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", start_new_session=True,
            )
        finally:
            if mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        yield process
    finally:
        if process is not None:
            with process:
                if process.returncode is None:
                    kill_owned_process(process)
