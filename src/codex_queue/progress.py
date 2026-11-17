"""Waiting display. Only rendering runs in a thread; callers keep their own thread."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import shutil
import sys
import threading
import time

from .language import tr
from .text_layout import fit, text_width


INTERVAL = 0.12
FRAMES = "|/-\\"
_target = ContextVar("progress_target", default=None)
_depth = ContextVar("progress_depth", default=0)
_lock = threading.RLock()
_active = []
_paused = 0
_width = 0


def spinner(now=None):
    return FRAMES[int((time.monotonic() if now is None else now) / INTERVAL) % len(FRAMES)]


def _write(stream, text):
    # A closed pipe or pythonw's absent stderr must never fail the operation.
    try:
        if stream is not None:
            stream.write(text)
            stream.flush()
    except (OSError, ValueError):
        pass


def _clear():
    global _width
    if _width and _active:
        _write(_active[-1][0], "\r" + " " * _width + "\r")
    _width = 0


def _draw():
    global _width
    if not _paused and _active:
        stream, label = _active[-1]
        width = max(1, shutil.get_terminal_size().columns - 1)
        text = fit(f"{spinner()} {label}", width)
        used = text_width(text)
        _write(stream, "\r" + text + " " * max(0, _width - used) + "\r")
        _width = used


@contextmanager
def output_to(stream):
    """Route progress past command output capture to the screen's stream."""
    token = _target.set(stream)
    try:
        yield
    finally:
        _target.reset(token)


@contextmanager
def suspended():
    """Keep prompts, editors and external login output free of animation."""
    global _paused
    with _lock:
        _clear()
        _paused += 1
    try:
        yield
    finally:
        with _lock:
            _paused -= 1
            _draw()


@contextmanager
def waiting(english, japanese):
    label = tr(english, japanese)
    stream = _target.get() or sys.stderr
    nested = _depth.get()
    token = _depth.set(nested + 1)
    stop = threading.Event()
    thread = None
    entry = None
    failed = True
    try:
        animated = bool(stream is not None and stream.isatty())
    except (OSError, ValueError):
        animated = False
    try:
        if animated:
            entry = (stream, label)
            with _lock:
                _clear()
                _active.append(entry)
                _draw()

            def animate():
                while not stop.wait(INTERVAL):
                    with _lock:
                        if _active and _active[-1] is entry:
                            _draw()

            thread = threading.Thread(target=animate, name="cq-progress", daemon=True)
            thread.start()
        elif not nested:
            with _lock:
                _write(stream, label + "...\n")
        yield
        failed = False
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        with _lock:
            if entry is not None:
                if _active[-1] is entry:
                    _clear()
                _active[:] = [item for item in _active if item is not entry]
                _draw()
            elif not nested:
                result = tr("interrupted or failed", "中断または失敗") if failed else tr("done", "完了")
                _write(stream, f"{label}: {result}\n")
        _depth.reset(token)


def progress(english, japanese):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with waiting(english, japanese):
                return function(*args, **kwargs)
        return run
    return decorate
