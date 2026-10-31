"""Bounded, in-memory AG-UI thread and SSE run management.

User commands arrive through SignalDeck's existing /run ActionDispatcher.
Only server-to-browser events use the plugin's read-only SSE route.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


MAX_MESSAGE_CHARS = 12_000
MAX_HISTORY_MESSAGES = 160
MAX_VISIBLE_TOOL_CALLS = 1024  # Bounded UI-only history, never sent to cli-agent.
MAX_RUN_EVENTS = 4096
MAX_EVENT_BYTES = 256 * 1024
CONNECTION_IDLE_SECONDS = 30 * 60
MAX_CONNECTIONS = 32


class ChatActionError(ValueError):
    """Expected, safely displayable error in a user action."""


@dataclass
class AgentRun:
    run_id: str
    thread_id: str
    payload: dict[str, Any] = field(repr=False)
    events: deque[tuple[int, dict[str, Any]]] = field(
        default_factory=lambda: deque(maxlen=MAX_RUN_EVENTS), repr=False
    )
    next_sequence: int = 1
    done: bool = False
    finalized: bool = False
    interrupted: bool = False
    failed: bool = False
    cancel_requested: bool = False
    answer: str = ""
    _message_fragments: dict[str, str] = field(default_factory=dict, repr=False)
    changed: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def emit(self, event: dict[str, Any]) -> None:
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("AG-UI event exceeds maximum size")
        with self.changed:
            self.events.append((self.next_sequence, event))
            self.next_sequence += 1
            kind = event.get("type")
            if kind == "TEXT_MESSAGE_START" and event.get("role") == "assistant":
                self._message_fragments[event["messageId"]] = ""
            elif kind == "TEXT_MESSAGE_CONTENT":
                message_id = event.get("messageId")
                if message_id in self._message_fragments:
                    self._message_fragments[message_id] += str(event.get("delta") or "")
            elif kind == "TEXT_MESSAGE_END":
                msg = self._message_fragments.pop(event.get("messageId"), None)
                if msg is not None:
                    self.answer += msg
            elif kind == "RUN_FINISHED":
                self.interrupted = event.get("outcome", {}).get("type") == "interrupt"
                self.done = True
            elif kind == "RUN_ERROR":
                self.failed = True
                self.done = True
            self.changed.notify_all()

    def finish(self) -> None:
        with self.changed:
            if not self.done:
                self.done = True
                self.changed.notify_all()


@dataclass
class AgentConnection:
    token: str = field(repr=False)
    view_key: str | None = None
    thread_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Keep display-only events separate from the agent's conversation context.
    display_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    tool_calls: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    runs: dict[str, AgentRun] = field(default_factory=dict, repr=False)
    current_run: str | None = None
    pending_interrupt: dict[str, Any] | None = field(default=None, repr=False)
    last_used: float = field(default_factory=time.monotonic)
    revoked: bool = False


class ChatRuntime:
    """One runtime per DisplayProcessor instance, not shared between users."""

    def __init__(
        self, endpoint: str, *,
        http_client_factory: Callable[..., httpx.Client] = httpx.Client,
    ):
        self.endpoint = endpoint
        self._client_factory = http_client_factory
        self._lock = threading.RLock()
        self._connections: dict[str, AgentConnection] = {}
        self._views: dict[str, str] = {}

    @staticmethod
    def normalize_view(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            if len(value) != 1:
                raise ChatActionError("Chat-View erwartet genau einen value.")
            value = value[0]
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ChatActionError("Chat-value muss eine nichtleere Zeichenkette sein.")
        return value

    def assert_view(self, key: str, identifier: str) -> None:
        with self._lock:
            connection = self._connection_locked(identifier)
            if connection.view_key != key:
                raise ChatActionError("Verbindung gehört zu einer anderen Chat-View.")

    def view_state(self, key: str) -> dict[str, Any]:
        """A shared view's conversation snapshot; never exposes the API token."""
        with self._lock:
            self._expire_locked()
            identifier = self._views.get(key)
            connection = self._connections.get(identifier) if identifier else None
            if connection is None or connection.revoked:
                return {"connected": False, "messages": [], "busy": False}

            connection.last_used = time.monotonic()
            messages = []
            for msg in connection.display_messages:
                if msg["role"] == "tools":
                    messages.append({
                        "role": "tools", "id": msg["id"],
                        "tools": [dict(tool) for tool in msg["tools"]],
                    })
                elif msg["role"] == "download":
                    messages.append({
                        "role": "download", "id": msg["id"],
                        "filename": msg["filename"], "mimeType": msg["mimeType"],
                    })
                else:
                    messages.append({"role": msg["role"], "content": msg["content"]})
            current = connection.runs.get(connection.current_run)
            busy = bool(current and not current.finalized)
            if busy:
                with current.changed:
                    partial = current.answer + "".join(current._message_fragments.values())
                if partial:
                    messages.append({"role": "assistant", "content": partial})
            pending = connection.pending_interrupt
            return {
                "connected": True,
                "connection_id": identifier,
                "messages": messages,
                "busy": busy,
                "pending_interrupt": ({
                    "id": pending.get("id"),
                    "message": pending.get("message"),
                    "reason": pending.get("reason"),
                } if pending else None),
            }

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for ident, connection in tuple(self._connections.items()):
            # A named view persists across navigation until explicit disconnect.
            if (connection.view_key is None
                    and now - connection.last_used > CONNECTION_IDLE_SECONDS):
                self._connections.pop(ident, None)
                connection.revoked = True
                # Expired credentials are discarded. Backend thread cleanup is
                # best effort and must not block the request holding this lock.
                self._spawn_cleanup(connection)

    def _connection_locked(self, identifier: str) -> AgentConnection:
        self._expire_locked()
        connection = self._connections.get(identifier)
        if connection is None or connection.revoked:
            raise ChatActionError("Chat-Verbindung nicht mehr aktiv. Bitte neu verbinden.")
        connection.last_used = time.monotonic()
        return connection

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        }

    def verify_and_connect(self, token: str, view_key: str | None = None) -> str:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ChatActionError("API-Token fehlt oder ist ungültig.")
        if view_key is not None:
            view_key = self.normalize_view(view_key)
            with self._lock:
                self._expire_locked()
                existing = self._views.get(view_key)
                if existing in self._connections:
                    return existing
        # cli-agent supports the optional capabilities discovery endpoint.
        # No connection state is created until the supplied credential works.
        try:
            with self._client_factory(timeout=8.0) as client:
                response = client.get(
                    self.endpoint.removesuffix("/ag-ui") + "/ag-ui/capabilities",
                    headers=self._headers(token),
                )
                if response.status_code in (401, 403):
                    raise ChatActionError("API-Token wurde vom Agenten abgelehnt.")
                response.raise_for_status()
                if not isinstance(response.json(), dict):
                    raise ChatActionError("Ungültige Agenten-Antwort.")
        except httpx.HTTPError as exc:
            raise ChatActionError("AG-UI-Agent nicht erreichbar.") from exc

        with self._lock:
            self._expire_locked()
            # Another browser may have connected during token verification.
            if view_key is not None:
                existing = self._views.get(view_key)
                if existing in self._connections:
                    return existing
            if len(self._connections) >= MAX_CONNECTIONS:
                raise ChatActionError("Zu viele aktive Chat-Verbindungen.")
            identifier = secrets.token_urlsafe(32)
            self._connections[identifier] = AgentConnection(
                token=token, view_key=view_key
            )
            if view_key is not None:
                self._views[view_key] = identifier
            return identifier

    def command_token(self) -> str:
        """Borrow the login from an active Chat UI of this processor.

        Different active tokens would make the target credentials ambiguous;
        fail closed rather than picking a view or browser arbitrarily.
        """
        with self._lock:
            self._expire_locked()
            tokens = {
                connection.token
                for connection in self._connections.values()
                if not connection.revoked and connection.view_key is not None
            }
            if not tokens:
                raise ChatActionError(
                    "LLM-Command nicht verfügbar: Zuerst in einer Chat-View "
                    "dieses Prozessors mit cli-agent verbinden."
                )
            if len(tokens) != 1:
                raise ChatActionError(
                    "LLM-Command nicht verfügbar: Mehrere verschiedene "
                    "API-Tokens in Chat-Views dieses Prozessors aktiv."
                )
            return next(iter(tokens))

    def contains(self, identifier: str) -> bool:
        with self._lock:
            try:
                self._connection_locked(identifier)
            except ChatActionError:
                return False
            return True

    def start_message(self, identifier: str, message: str) -> str:
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            raise ChatActionError("Nachricht fehlt oder ist zu lang.")
        with self._lock:
            connection = self._connection_locked(identifier)
            self._assert_idle(connection)
            if connection.pending_interrupt:
                raise ChatActionError("Zunächst die ausstehende Tool-Freigabe beantworten.")
            if len(connection.messages) >= MAX_HISTORY_MESSAGES:
                raise ChatActionError("Chat-History-Limit erreicht. Bitte neuen Chat starten.")
            # The preceding question's artifacts expire when a new one starts.
            # Do not leave stale download controls in the visible transcript.
            connection.display_messages[:] = [
                item for item in connection.display_messages
                if item.get("role") != "download"
            ]
            connection.messages.append({
                "id": uuid.uuid4().hex,
                "role": "user",
                "content": message.strip(),
            })
            connection.display_messages.append({
                "role": "user", "content": message.strip(),
            })
            return self._submit_locked(connection)

    def cancel(self, identifier: str) -> str:
        """Request cancellation of the active run without deleting its thread."""
        with self._lock:
            connection = self._connection_locked(identifier)
            run = connection.runs.get(connection.current_run or "")
            if run is None or run.done:
                raise ChatActionError("Kein aktiver Agentenlauf vorhanden.")
            if run.cancel_requested:
                return run.run_id
            # Flag first: a running reader must not accept a late success
            # after the user has requested cancellation.
            run.cancel_requested = True
            token = connection.token
            thread_id = run.thread_id
            run_id = run.run_id
        try:
            with self._client_factory(timeout=8.0) as client:
                response = client.post(
                    self.endpoint,
                    headers={**self._headers(token), "Content-Type": "application/json"},
                    json={"action": "cancel", "threadId": thread_id, "runId": run_id},
                )
            if response.status_code != 202:
                raise ChatActionError(
                    f"Agent hat den Abbruch abgelehnt (HTTP {response.status_code})."
                )
        except httpx.HTTPError as exc:
            with self._lock:
                run.cancel_requested = False
            raise ChatActionError("Agent für Abbruch nicht erreichbar.") from exc
        return run_id

    def approve(self, identifier: str, interrupt_id: str, decision: str) -> str:
        if decision not in {"yes", "no", "session"}:
            raise ChatActionError("Ungültige Freigabeentscheidung.")
        with self._lock:
            connection = self._connection_locked(identifier)
            self._assert_idle(connection)
            pending = connection.pending_interrupt
            if pending is None or pending.get("id") != interrupt_id:
                raise ChatActionError("Keine passende ausstehende Tool-Freigabe.")
            resume = [{
                "interruptId": interrupt_id,
                "status": "resolved",
                "payload": {
                    "approved": decision != "no",
                    "rememberForSession": decision == "session",
                },
            }]
            connection.pending_interrupt = None
            return self._submit_locked(connection, resume=resume)

    def _assert_idle(self, connection: AgentConnection) -> None:
        if connection.current_run is not None:
            existing = connection.runs.get(connection.current_run)
            if existing is not None and not existing.done:
                raise ChatActionError("Agent bearbeitet bereits eine Anfrage.")

    def _submit_locked(
        self, connection: AgentConnection, *, resume: list[dict[str, Any]] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        payload = {
            "protocolVersion": "1.0",
            "threadId": connection.thread_id,
            "runId": run_id,
            "messages": [dict(msg) for msg in connection.messages],
        }
        if resume is not None:
            payload["resume"] = resume
        run = AgentRun(run_id=run_id, thread_id=connection.thread_id, payload=payload)
        connection.current_run = run_id
        connection.runs = {run_id: run}  # keep one bounded run replay per chat
        worker = threading.Thread(
            target=self._worker, args=(connection, run), daemon=True,
            name="signaldeck-agui-" + run_id[:8],
        )
        worker.start()
        return run_id

    def _worker(self, connection: AgentConnection, run: AgentRun) -> None:
        try:
            timeout = httpx.Timeout(connect=8.0, read=None, write=20.0, pool=8.0)
            with self._client_factory(timeout=timeout) as client:
                with client.stream(
                    "POST", self.endpoint,
                    headers={**self._headers(connection.token), "Content-Type": "application/json"},
                    json=run.payload,
                ) as response:
                    if response.status_code != 200:
                        raise ChatActionError(
                            f"Agent hat die Anfrage abgelehnt (HTTP {response.status_code})."
                        )
                    lines: list[str] = []
                    for line in response.iter_lines():
                        if connection.revoked:
                            return
                        if line == "":
                            if lines:
                                self._receive_frame(connection, run, lines)
                                lines = []
                        elif line.startswith("data:"):
                            lines.append(line[5:].lstrip(" "))
                    if lines:
                        self._receive_frame(connection, run, lines)
            if not run.done:
                run.emit({"type": "RUN_ERROR", "message": "AG-UI-Stream unerwartet beendet."})
        except Exception:
            if not run.done and not connection.revoked:
                run.emit({"type": "RUN_ERROR", "message": "AG-UI-Agentenlauf fehlgeschlagen."})
        finally:
            with self._lock:
                if not connection.revoked and connection.current_run == run.run_id:
                    if run.failed or not run.done:
                        self._record_tool_activity(
                            connection, run, {"type": "RUN_ERROR"},
                        )
                    if run.done and not run.failed:
                        if run.answer and not run.interrupted:
                            connection.messages.append({
                                "id": uuid.uuid4().hex,
                                "role": "assistant",
                                "content": run.answer[:100_000],
                            })
                            connection.display_messages.append({
                                "role": "assistant",
                                "content": run.answer[:100_000],
                            })
                    else:
                        # Reset ambiguous history rather than retrying an
                        # action whose previous execution status is unknown.
                        connection.pending_interrupt = None
                run.finish()
                run.finalized = True

    def _receive_frame(self, connection: AgentConnection, run: AgentRun, lines: list[str]):
        try:
            event = json.loads("\n".join(lines))
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Malformed AG-UI event")
            if event.get("type") == "RUN_FINISHED":
                outcome = event.get("outcome") or {}
                if outcome.get("type") == "interrupt":
                    interrupts = outcome.get("interrupts") or []
                    with self._lock:
                        if not connection.revoked and connection.current_run == run.run_id:
                            connection.pending_interrupt = interrupts[0] if interrupts else None
            with self._lock:
                if not connection.revoked and connection.current_run == run.run_id:
                    self._record_tool_activity(connection, run, event)
                    if event.get("type") == "TOOL_CALL_RESULT":
                        parts = event.get("content")
                        if isinstance(parts, list):
                            for part in parts:
                                if not isinstance(part, dict) or part.get("type") != "document":
                                    continue
                                source = part.get("source")
                                metadata = part.get("metadata")
                                if not isinstance(source, dict) or not isinstance(metadata, dict):
                                    continue
                                identifier = metadata.get("artifactId")
                                filename = metadata.get("filename")
                                mime_type = source.get("mimeType")
                                expected_url = (
                                    "/threads/" + connection.thread_id + "/artifacts/" + identifier
                                    if isinstance(identifier, str) else ""
                                )
                                if (isinstance(identifier, str) and len(identifier) == 32
                                        and all(ch in "0123456789abcdef" for ch in identifier)
                                        and source.get("type") == "url"
                                        and source.get("value") == expected_url
                                        and isinstance(filename, str) and 0 < len(filename) <= 128
                                        and isinstance(mime_type, str) and len(mime_type) <= 100):
                                    connection.display_messages.append({
                                        "role": "download", "id": identifier,
                                        "filename": filename, "mimeType": mime_type,
                                    })
            run.emit(event)
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            raise ChatActionError("Ungültiges AG-UI-Event.") from exc

    @staticmethod
    def _record_tool_activity(
        connection: AgentConnection, run: AgentRun, event: dict[str, Any],
    ) -> None:
        """Keep a bounded, display-only tool timeline from existing AG-UI events.

        Store ONLY the MCP tool name and its lifecycle status. Never persist
        arguments, responses, or approval metadata in the chat transcript.
        The agent's own conversation payload remains completely unchanged.
        """
        kind = event.get("type")
        call_id = event.get("toolCallId")
        valid_id = isinstance(call_id, str) and 0 < len(call_id) <= 256
        if kind == "TOOL_CALL_START" and valid_id:
            existing = connection.tool_calls.get(call_id)
            if existing is not None:
                existing["status"] = "running"
                return
            if len(connection.tool_calls) >= MAX_VISIBLE_TOOL_CALLS:
                return
            name = event.get("toolCallName")
            if not isinstance(name, str) or not name.strip():
                name = "Unbekanntes Tool"
            # The tool name comes from the agent; avoid unbounded UI strings.
            tool = {"id": call_id, "name": name[:256], "status": "running"}
            group = next((
                item for item in reversed(connection.display_messages)
                if item["role"] == "tools" and item["id"] == run.run_id
            ), None)
            if group is None:
                group = {"role": "tools", "id": run.run_id, "tools": []}
                connection.display_messages.append(group)
            group["tools"].append(tool)
            connection.tool_calls[call_id] = tool
        elif kind == "TOOL_CALL_RESULT" and valid_id:
            tool = connection.tool_calls.get(call_id)
            if tool is not None:
                status = event.get("content")
                tool["status"] = (
                    status if isinstance(status, str) and status in {"success", "error", "denied", "finished"}
                    else "finished"
                )
        elif kind == "RUN_FINISHED":
            outcome = event.get("outcome") or {}
            if isinstance(outcome, dict) and outcome.get("type") == "interrupt":
                for interrupt in outcome.get("interrupts") or []:
                    if not isinstance(interrupt, dict):
                        continue
                    requested = interrupt.get("toolCallId")
                    tool = (
                        connection.tool_calls.get(requested)
                        if isinstance(requested, str) else None
                    )
                    if tool is not None:
                        tool["status"] = "approval"
            else:
                for group in connection.display_messages:
                    if group["role"] == "tools" and group["id"] == run.run_id:
                        for tool in group["tools"]:
                            if tool["status"] == "running":
                                tool["status"] = "finished"
        elif kind == "RUN_ERROR":
            for group in connection.display_messages:
                if group["role"] == "tools" and group["id"] == run.run_id:
                    for tool in group["tools"]:
                        if tool["status"] == "running":
                            tool["status"] = "error"

    def download_artifact(self, identifier: str, artifact_id: str) -> tuple[bytes, str, str]:
        """Proxy a published cli-agent artifact using the server-side credential."""
        if not isinstance(artifact_id, str) or len(artifact_id) != 32 or not all(
            ch in "0123456789abcdef" for ch in artifact_id
        ):
            raise ChatActionError("Ungültige Download-ID.")
        with self._lock:
            connection = self._connection_locked(identifier)
            token, thread_id = connection.token, connection.thread_id
            published = next((
                msg for msg in connection.display_messages
                if msg.get("role") == "download" and msg.get("id") == artifact_id
            ), None)
            if published is None:
                raise ChatActionError("Download ist nicht für diese Konversation freigegeben.")
            filename = published["filename"]
            mime_type = published["mimeType"]
        url = self.endpoint.removesuffix("/ag-ui") + (
            "/threads/" + thread_id + "/artifacts/" + artifact_id
        )
        with self._client_factory(timeout=30.0) as client:
            response = client.get(url, headers=self._headers(token))
            if response.status_code != 200:
                raise ChatActionError("Download nicht mehr verfügbar.")
            if len(response.content) > 20_000_000:
                raise ChatActionError("Download zu groß.")
            return response.content, filename, mime_type

    def get_run(self, identifier: str, run_id: str) -> AgentRun:
        with self._lock:
            connection = self._connection_locked(identifier)
            run = connection.runs.get(run_id)
            if run is None:
                raise ChatActionError("AG-UI-Run nicht gefunden.")
            return run

    def new_chat(self, identifier: str) -> None:
        with self._lock:
            connection = self._connection_locked(identifier)
            self._assert_idle(connection)
            previous_thread = connection.thread_id
            connection.thread_id = uuid.uuid4().hex
            connection.messages = []
            connection.display_messages.clear()
            connection.tool_calls.clear()
            connection.pending_interrupt = None
            connection.current_run = None
            connection.runs.clear()
            self._spawn_cleanup(connection, previous_thread)

    def disconnect(self, identifier: str) -> None:
        with self._lock:
            connection = self._connections.pop(identifier, None)
            if connection is None:
                raise ChatActionError("Chat-Verbindung nicht gefunden.")
            connection.revoked = True
            if (connection.view_key is not None
                    and self._views.get(connection.view_key) == identifier):
                self._views.pop(connection.view_key, None)
            self._spawn_cleanup(connection)

    def _spawn_cleanup(self, connection: AgentConnection, thread_id: str | None = None):
        token = connection.token
        thread = thread_id or connection.thread_id

        def cleanup():
            try:
                with self._client_factory(timeout=8.0) as client:
                    client.delete(
                        self.endpoint.removesuffix("/ag-ui") + "/threads/" + thread,
                        headers=self._headers(token),
                    )
            except httpx.HTTPError:
                pass

        threading.Thread(target=cleanup, daemon=True, name="signaldeck-agui-cleanup").start()

    def shutdown(self) -> None:
        with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
            self._views.clear()
            for connection in connections:
                connection.revoked = True
                self._spawn_cleanup(connection)
