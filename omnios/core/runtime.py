"""
runtime.py — Async actor runtime with an OTP-style supervision tree.
====================================================================
Actors are independent asyncio tasks, each with a bounded mailbox (backpressure).
Communication is by typed message passing — `ask` (request/response via a Future)
and `tell` (fire-and-forget). A Supervisor monitors actors and, on an *unexpected*
crash, restarts them with bounded exponential backoff ("let it crash").

Business failures (e.g. a slot conflict) are NOT crashes — handlers return them as
values, so only genuine bugs trip the supervisor. Self-test: `python runtime.py`.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional


@dataclass
class Message:
    type: str
    payload: Any = None
    reply_to: Optional["asyncio.Future"] = None


Handler = Callable[[Message], Awaitable[Any]]


class Actor:
    def __init__(self, name: str, handler: Handler, mailbox_size: int = 256) -> None:
        self.name = name
        self.handler = handler
        self.mailbox: "asyncio.Queue[Message]" = asyncio.Queue(maxsize=mailbox_size)
        self.restarts = 0
        self.processed = 0
        self._task: Optional[asyncio.Task] = None
        self._last_crash: Optional[float] = None

    async def _run(self) -> None:
        while True:
            msg = await self.mailbox.get()
            try:
                result = await self.handler(msg)
                if msg.reply_to is not None and not msg.reply_to.done():
                    msg.reply_to.set_result(result)
                self.processed += 1
            except Exception as exc:  # genuine bug → fail the future, then crash
                if msg.reply_to is not None and not msg.reply_to.done():
                    msg.reply_to.set_exception(exc)
                raise
            finally:
                self.mailbox.task_done()

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(), name=f"actor:{self.name}")
        return self._task

    async def ask(self, type_: str, payload: Any = None, timeout: float = 8.0) -> Any:
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future" = loop.create_future()
        await self.mailbox.put(Message(type_, payload, fut))
        return await asyncio.wait_for(fut, timeout)

    async def tell(self, type_: str, payload: Any = None) -> None:
        await self.mailbox.put(Message(type_, payload))


class Supervisor:
    """One-for-one supervision: each actor restarted independently on crash."""

    def __init__(self, max_restarts: int = 5, backoff: float = 0.1,
                 restart_window: float = 60.0, max_backoff: float = 5.0) -> None:
        self.actors: Dict[str, Actor] = {}
        self.max_restarts = max_restarts
        self.backoff = backoff
        self.restart_window = restart_window   # healthy for this long → restart budget resets (OTP intensity window)
        self.max_backoff = max_backoff         # cap so backoff never explodes
        self.dead_letters: list[str] = []      # actors that exhausted their restart budget
        self._monitor: Optional[asyncio.Task] = None  # kept for API compat (unused: supervision is event-driven)
        self._running = False

    def register(self, actor: Actor) -> Actor:
        self.actors[actor.name] = actor
        return actor

    def start_all(self) -> None:
        self._running = True
        for a in self.actors.values():
            self._spawn(a)

    def _spawn(self, a: Actor) -> None:
        t = a.start()
        # Event-driven supervision: restart is triggered the instant a task dies — no 150ms poll latency.
        t.add_done_callback(lambda task, actor=a: self._on_exit(actor, task))

    def _on_exit(self, a: Actor, task: "asyncio.Task") -> None:
        if not self._running or task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return  # clean exit (the _run loop only ends on cancellation)
        now = asyncio.get_running_loop().time()
        # A long-healthy actor earns its restart budget back (sliding intensity window).
        if a._last_crash is not None and (now - a._last_crash) > self.restart_window:
            a.restarts = 0
        a._last_crash = now
        if a.restarts < self.max_restarts:
            a.restarts += 1
            delay = min(self.backoff * (2 ** (a.restarts - 1)), self.max_backoff)  # true exponential
            delay += random.uniform(0.0, delay * 0.25)                             # jitter — no thundering herd
            asyncio.create_task(self._restart_after(a, delay, exc))
        else:
            self.dead_letters.append(a.name)
            print(f"[SUPERVISOR] DEAD-LETTER actor '{a.name}' exhausted "
                  f"{self.max_restarts} restarts after {exc.__class__.__name__}: {exc}")

    async def _restart_after(self, a: Actor, delay: float, exc: BaseException) -> None:
        await asyncio.sleep(delay)
        if self._running:
            print(f"[SUPERVISOR] restarting actor '{a.name}' "
                  f"(restart {a.restarts}/{self.max_restarts}, +{delay:.2f}s) after {exc.__class__.__name__}")
            self._spawn(a)

    async def shutdown(self, drain_timeout: float = 2.0) -> None:
        """Graceful drain: stop supervising, let in-flight mailbox items finish, then cancel."""
        self._running = False
        try:
            await asyncio.wait_for(
                asyncio.gather(*(a.mailbox.join() for a in self.actors.values())),
                timeout=drain_timeout,
            )
        except Exception:
            pass  # a crashed/stuck actor won't drain — the timeout covers it
        for a in self.actors.values():
            if a._task:
                a._task.cancel()


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # self-test: message passing + supervised restart
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async def main():
        sup = Supervisor(max_restarts=3, backoff=0.05)

        async def echo(msg: Message):
            return f"echo:{msg.payload}"

        state = {"n": 0}

        async def flaky(msg: Message):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")  # crash once
            return f"recovered({msg.payload})"

        e = sup.register(Actor("echo", echo))
        f = sup.register(Actor("flaky", flaky))
        sup.start_all()

        print("echo ->", await e.ask("x", "hello"))
        try:
            await f.ask("x", "1", timeout=2)
        except Exception as ex:
            print("flaky first ask raised (expected):", ex.__class__.__name__)
        await asyncio.sleep(0.5)  # let supervisor restart it
        print("flaky after restart ->", await f.ask("x", "2", timeout=2))
        assert f.restarts == 1, f"expected 1 restart, got {f.restarts}"
        await sup.shutdown()
        print("✅ runtime self-test passed (message passing + supervised restart)")

    asyncio.run(main())
