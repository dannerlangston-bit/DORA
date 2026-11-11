"""The single writer thread of a Sentinel database (8.2 Single-writer rule).

8.2: the Orchestrator of a shard writes its Run Store "through one writer thread", which "drains a queue of
batches, one checkpoint per batch"; the Reporter service writes the Central Store through one writer thread too.
`WriterService` is that thread: it opens the `Store` itself (an SQLite connection belongs to the thread that
opened it), then runs the submitted work items one at a time in submission order. Producers on any thread (the
drop-log drainer, Capture, the Reporter's request handlers) call `submit(fn, *args)` and get a
`concurrent.futures.Future` with `fn(store, *args)`'s result or its exception (StoreRefused, ImportConflict,
StoreWriteFailed...), so a refused batch raises in its own submitter and nowhere else; `call` waits for it.

Choices where the spec is silent (reported):
- One FIFO queue: items run in the order they were queued, which keeps every producer's own order; a work item is
  one `Store` call, and each Store write method is one batch (one BEGIN IMMEDIATE transaction).
- `close()` drains what is already queued, then closes the store; submitting after `close()` is refused.
- A failure to open the store (writer lock held, integrity check, migration) is raised by `start()`.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable, TypeVar

from .store import Store, StoreError

T = TypeVar("T")
_STOP = object()


class WriterService:
    """One writer thread over one `Store` (module docstring). Use as a context manager, or `start()` and
    `close()`."""

    def __init__(self, open_store: Callable[[], Store], *, name: str = "sentinel-writer"):
        """`open_store` builds the Store; it runs on the writer thread."""
        self._open = open_store
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closed = False
        self._lock = threading.Lock()

    def start(self) -> "WriterService":
        """Start the thread and wait until it has opened the store; an open failure is raised here."""
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            self._thread.join()
            raise self._error
        return self

    def __enter__(self) -> "WriterService":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        """Queue `fn(store, *args, **kwargs)` for the writer thread; returns its Future."""
        fut: Future[T] = Future()
        with self._lock:
            if self._closed or not self._thread.is_alive():
                raise StoreError("the writer service is not running (8.2)")
            self._queue.put((fut, fn, args, kwargs))
        return fut

    def call(self, fn: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any) -> T:
        """`submit` and wait for the result (or the exception) of the work item."""
        return self.submit(fn, *args, **kwargs).result(timeout)

    def close(self, timeout: float | None = None) -> None:
        """Run what is queued, close the store and stop the thread. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._thread.is_alive():
                self._queue.put(_STOP)
        if self._thread.ident is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            store = self._open()
        except BaseException as e:            # raised again by start() on the caller's thread
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                fut, fn, args, kwargs = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    fut.set_result(fn(store, *args, **kwargs))
                except BaseException as e:    # the submitter sees it; the writer keeps draining
                    fut.set_exception(e)
        finally:
            store.close()
