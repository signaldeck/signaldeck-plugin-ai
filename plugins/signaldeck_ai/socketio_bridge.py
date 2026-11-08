"""Serialized Socket.IO AG-UI requests with targeted cancellation and status.

Inbound chat is routed by SignalDeck's existing MessageBus. Control messages
are handled immediately rather than placed behind waiting agent questions.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .chat_runtime import ChatActionError, ChatRuntime, MAX_MESSAGE_CHARS
from .llm_command import LLMCommand

_LOG = logging.getLogger(__name__)


@dataclass
class BridgeRequest:
    request_id: str
    response_source: str
    room: str
    prompt: str = field(repr=False)
    cancelled: bool = False
    cancel_requested: bool = False
    cancel_accepted: bool = False
    cancel_error: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    token: str | None = field(default=None, repr=False)
    cancel_started: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    cancel_finished: threading.Event = field(default_factory=threading.Event, repr=False)


class SocketIOChatBridge:
    def __init__(
        self, config: dict[str, Any], ctx: Any, runtime: ChatRuntime, *,
        ask: Callable[[str, str], Any] | None = None,
    ):
        if not isinstance(config, dict) or set(config) - {
            "routes", "max_queue", "timeout_seconds"
        }:
            raise ValueError("Invalid socketio_bridge configuration")
        routes = config.get("routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError("socketio_bridge.routes requires at least one route")
        self.routes: dict[tuple[str, str], str] = {}
        for route in routes:
            if not isinstance(route, dict) or set(route) != {
                "room", "source", "response_source"
            }:
                raise ValueError(
                    "Each socketio_bridge route requires room, source, response_source"
                )
            if any(not isinstance(v, str) or not v.strip() or v != v.strip()
                   for v in route.values()):
                raise ValueError("Invalid socketio_bridge route")
            key = (route["room"], route["source"])
            if key in self.routes:
                raise ValueError("Duplicate socketio_bridge room/source route")
            self.routes[key] = route["response_source"]

        max_queue = config.get("max_queue", 5)
        if type(max_queue) is not int or not 0 <= max_queue <= 100:
            raise ValueError("socketio_bridge.max_queue must be 0..100")
        timeout = config.get("timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("socketio_bridge.timeout_seconds must be 1..600")

        self.ctx = ctx
        self.runtime = runtime
        self._slots = threading.BoundedSemaphore(max_queue + 1)
        self._stopped = threading.Event()
        self._guard = threading.RLock()
        # A bounded deque allows a cancelled waiting request to free its
        # capacity immediately, without racing the worker's dequeue.
        self._queue: deque[BridgeRequest] = deque()
        self._queue_ready = threading.Condition(self._guard)
        self._active: BridgeRequest | None = None
        self._seen: set[tuple[str, str]] = set()
        self._seen_order: deque[tuple[str, str]] = deque()
        self._ask = ask
        self._command = LLMCommand("socketio_bridge_oneshot", runtime, timeout_seconds=timeout)
        self._unsubscribe = ctx.message_bus.subscribe(self.on_message)
        self._worker = threading.Thread(
            target=self._run, name="signaldeck-socketio-ai", daemon=True
        )
        self._worker.start()

    def _publish(self, source: str, content: str, room: str) -> None:
        if not self._stopped.is_set():
            self.ctx.message(
                source=source, content=content,
                metadata={"type": "chat", "content_type": "text", "system": False},
            )

    def status(self, room: str | None = None) -> dict[str, Any]:
        """Bounded, token-free status for the SignalDeck view and chat commands."""
        with self._guard:
            active = self._active
            if active is not None and room is not None and active.room != room:
                active = None
            waiting = [
                {"id": item.request_id, "room": item.room,
                 "prompt": item.prompt[:160]}
                for item in self._queue
                if room is None or item.room == room
            ]
            return {
                "active": ({
                    "id": active.request_id,
                    "room": active.room,
                    "prompt": active.prompt[:160],
                    "cancelling": active.cancel_requested,
                    "cancel_error": active.cancel_error,
                } if active is not None else None),
                "waiting": waiting,
                "waiting_count": len(waiting),
            }

    def _cancel_upstream(self, item: BridgeRequest) -> None:
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.post(
                    self.runtime.endpoint,
                    headers={
                        **self.runtime._headers(item.token or ""),
                        "Content-Type": "application/json",
                    },
                    json={"action": "cancel", "threadId": item.thread_id, "runId": item.run_id},
                )
            if response.status_code != 202:
                raise RuntimeError(f"HTTP {response.status_code}")
        except Exception:
            _LOG.exception("Socket.IO cancellation failed (request=%s)", item.request_id)
            with self._guard:
                item.cancel_error = "Der Agent hat den Abbruch nicht bestätigt."
                item.cancel_requested = False
                item.cancel_started = False
                item.cancel_finished.set()
            return
        with self._guard:
            item.cancel_accepted = True
            item.stop_event.set()
            item.cancel_finished.set()

    def _start_cancel_locked(self, item: BridgeRequest) -> None:
        if (item.cancel_started or item.cancel_accepted or item.thread_id is None
                or item.run_id is None or item.token is None):
            return
        item.cancel_finished.clear()
        item.cancel_started = True
        threading.Thread(
            target=self._cancel_upstream, args=(item,),
            name="signaldeck-socketio-cancel", daemon=True,
        ).start()

    def _on_started(self, item: BridgeRequest, thread_id: str, run_id: str) -> None:
        with self._guard:
            item.thread_id, item.run_id = thread_id, run_id
            if item.cancel_requested:
                self._start_cancel_locked(item)

    def cancel(self, *, room: str | None = None, request_id: str | None = None) -> str:
        """Request cancellation for the active run or a specified waiting request."""
        with self._guard:
            if self._stopped.is_set():
                return "Der Haus-Assistent wird beendet."
            item = self._active
            if item is not None and (room is None or item.room == room) and (
                request_id is None or item.request_id == request_id
            ):
                if item.cancel_requested:
                    return "Abbruch bereits angefordert."
                item.cancel_error = None
                item.cancel_requested = True
                # The injected test adapter has no upstream AG-UI run.
                if self._ask is not None:
                    item.cancel_accepted = True
                    item.stop_event.set()
                else:
                    self._start_cancel_locked(item)
                return "Abbruch angefordert."
            if request_id is not None:
                for index, pending in enumerate(self._queue):
                    if pending.request_id == request_id and (
                        room is None or pending.room == room
                    ):
                        # Guard also protects dequeuing. The worker cannot
                        # acquire this request or release its slot twice.
                        del self._queue[index]
                        self._slots.release()
                        return "Wartende Anfrage abgebrochen."
            return "Keine passende laufende Anfrage vorhanden."

    def on_message(self, message: Any) -> None:
        meta = message.metadata or {}
        if not isinstance(meta, Mapping):
            return
        room = meta.get("room")
        response_source = self.routes.get((room, message.source))
        if response_source is None or not isinstance(message.content, str):
            return
        prompt = message.content.strip()
        if not prompt or len(prompt) > MAX_MESSAGE_CHARS:
            return

        event_id = meta.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return
        key = (room, event_id)
        with self._guard:
            if self._stopped.is_set() or key in self._seen:
                return
            self._seen.add(key)
            self._seen_order.append(key)
            while len(self._seen_order) > 1024:
                self._seen.discard(self._seen_order.popleft())

            # Commands bypass the queue. A room may only stop its own run.
            if prompt.casefold() == "stop":
                self._publish(response_source, self.cancel(room=room), room)
                return
            if prompt.casefold() == "status":
                state = self.status(room)
                active = state["active"]
                description = (
                    ("Abbruch fehlgeschlagen; Anfrage läuft" if active["cancel_error"]
                     else "Abbruch läuft" if active["cancelling"]
                     else "Eine Anfrage läuft")
                    if active else "Keine Anfrage läuft"
                )
                self._publish(
                    response_source,
                    f"{description}; {state['waiting_count']} weitere Anfragen warten.",
                    room,
                )
                return
            if not self._slots.acquire(blocking=False):
                self._publish(
                    response_source,
                    "Der Haus-Assistent ist ausgelastet. Bitte später erneut versuchen.",
                    room,
                )
                return
            item = BridgeRequest(event_id, response_source, room, prompt)
            self._queue.append(item)
            self._queue_ready.notify()

    def _run(self) -> None:
        while True:
            with self._queue_ready:
                while not self._queue and not self._stopped.is_set():
                    self._queue_ready.wait()
                if self._stopped.is_set():
                    return
                item = self._queue.popleft()
                self._active = item
            try:
                answer = None
                try:
                    token = self.runtime.command_token()
                    with self._guard:
                        item.token = token
                    if self._ask is None:
                        answer = asyncio.run(
                            self._command._ask_once(
                                item.prompt, token, stop_event=item.stop_event,
                                on_started=lambda thread, run: self._on_started(
                                    item, thread, run
                                ),
                            )
                        )
                    else:
                        answer = asyncio.run(self._ask(item.prompt, token))
                except asyncio.CancelledError:
                    if not item.cancel_requested:
                        _LOG.warning("Socket.IO run cancelled unexpectedly")
                except ChatActionError:
                    answer = "Haus-Assistent nicht verbunden. Bitte zuerst in einer AI-Chat-View anmelden."
                except Exception:
                    _LOG.exception("Socket.IO AI one-shot failed for room=%s", item.room)
                    answer = "Der Haus-Assistent konnte die Anfrage nicht bearbeiten."
                # The AG-UI run may succeed while cancellation is still in
                # flight. Resolve the HTTP cancellation attempt before deciding
                # whether to discard its answer. Do not hold _guard while
                # waiting; the cancellation worker needs it to signal completion.
                with self._guard:
                    pending_cancel = item.cancel_started and not item.cancel_accepted
                if pending_cancel:
                    # httpx has an 8-second timeout; cap this wait too so an
                    # unresponsive cancellation cannot block subsequent jobs.
                    item.cancel_finished.wait(timeout=12.0)
                with self._guard:
                    if item.cancel_accepted:
                        answer = "Anfrage abgebrochen."
                    elif item.cancel_requested and answer is None:
                        answer = "Anfrage beendet; der Abbruch wurde nicht bestätigt."
                    # A failed/rejected cancellation must preserve the actual
                    # successful answer. Late 202 responses cannot erase it.
                    if answer:
                        self._publish(item.response_source, answer, item.room)
            finally:
                with self._guard:
                    if self._active is item:
                        self._active = None
                    item.token = None
                self._slots.release()

    def shutdown(self) -> None:
        with self._queue_ready:
            self._stopped.set()
            self._unsubscribe()
            if self._active is not None:
                self._active.stop_event.set()
            while self._queue:
                self._queue.popleft()
                self._slots.release()
            self._queue_ready.notify_all()
        self._worker.join(timeout=2.0)
