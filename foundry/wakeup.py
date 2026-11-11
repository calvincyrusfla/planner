"""A thread-safe wake signal: any thread can cut short what one coroutine is waiting for."""

from __future__ import annotations

import asyncio
import threading

__all__ = ["Wakeup"]


class Wakeup:
    """Wakes the coroutine in :meth:`wait` when :meth:`notify` is called, from any thread.

    A wake is never lost: one that arrives while nobody waits is returned by the next
    :meth:`wait`, and several wakes between two waits count as one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._reason: str | None = None

    def notify(self, reason: str, task_id: str | None = None) -> None:
        """Record a wake and rouse the waiter.

        ``task_id`` is ignored; it lets this method be a ``FoundryQueue.on_event`` callback.
        """
        with self._lock:
            if self._reason is None:
                self._reason = reason
            loop, event = self._loop, self._event
        if loop is None or event is None:
            return  # no loop yet: the first wait finds the pending reason
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # the loop is closed: nobody is left to wake
            pass

    async def wait(self, timeout: float) -> str | None:
        """Wait up to ``timeout`` seconds; the first reason since the last wait, or None on timeout."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not loop or self._event is None:
                self._loop, self._event = loop, asyncio.Event()
            event = self._event
        deadline = loop.time() + timeout
        while True:
            # A set() left over from a wake that was already taken lands here and just goes round again.
            reason = self._take(event)
            remaining = deadline - loop.time()
            if reason is not None or remaining <= 0:
                return reason
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                return self._take(event)

    def _take(self, event: asyncio.Event) -> str | None:
        """Clear the event and hand over the pending reason, as one step against :meth:`notify`."""
        with self._lock:
            event.clear()
            reason, self._reason = self._reason, None
        return reason
