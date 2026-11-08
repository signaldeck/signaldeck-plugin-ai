"""A one-shot SignalDeck Cmd command using a temporary cli-agent AG-UI thread.

The credential is borrowed from the matching ChatRuntime at invocation time:
the command never reads environment variables or stores a separate token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable

import httpx
from signaldeck_sdk import Command

from .chat_runtime import ChatRuntime, MAX_MESSAGE_CHARS

_LOG = logging.getLogger(__name__)


class LLMCommand(Command):
    """Execute one prompt with no shared conversation history or approvals."""

    # Used by Cmd logging when the SDK supports suppressing sensitive inputs.
    redact_arguments = True

    def __init__(
        self,
        name: str,
        runtime: ChatRuntime,
        *,
        timeout_seconds: int = 1200,
        async_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ):
        if not isinstance(name, str) or not name.strip() or " " in name:
            raise ValueError("LLM Cmd name must be a nonempty single token")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1200:
            raise ValueError("LLM Cmd timeout_seconds must be between 1 and 1200")
        super().__init__(name, "Prompt cli-agent using a fresh temporary session")
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self._client_factory = async_client_factory

    async def run(self, *args, cmdRes=None, stopEvent=None):
        prompt = " ".join(str(arg) for arg in args).strip()
        if not prompt or len(prompt) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"LLM prompt must contain 1 to {MAX_MESSAGE_CHARS} characters"
            )
        if stopEvent is not None and stopEvent.is_set():
            raise asyncio.CancelledError()

        # Must be backed by a Chat UI connection of THIS processor instance.
        # Fail before creating a cli-agent session when nobody authenticated.
        token = self.runtime.command_token()
        answer = await self._ask_once(prompt, token, stop_event=stopEvent)
        if cmdRes is not None:
            cmdRes.appendState(self, msg=answer)

    async def _ask_once(self, prompt: str, token: str, *, stop_event=None, on_started=None) -> str:
        thread_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        endpoint = self.runtime.endpoint
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "protocolVersion": "1.0",
            "threadId": thread_id,
            "runId": run_id,
            "messages": [{
                "id": uuid.uuid4().hex, "role": "user", "content": prompt,
            }],
        }

        # A new thread is automatically initialized by cli-agent on POST.
        # We do not reuse any chat thread or prompt-history from a UI view.
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._client_factory(
                    timeout=httpx.Timeout(connect=8, read=None, write=20, pool=8)
                ) as client:
                    async with client.stream(
                        "POST", endpoint, headers=headers, json=payload,
                    ) as response:
                        response.raise_for_status()
                        # The backend now knows this thread/run pair. Expose it
                        # to the Socket.IO bridge for targeted cancellation.
                        if on_started is not None:
                            on_started(thread_id, run_id)
                        return await self._read_answer(response, stop_event=stop_event)
        finally:
            # Always attempt deletion, even on approval, timeout or cancellation.
            # Run cleanup separately so a cancelled Cmd task does not abandon
            # the cli-agent session before its MCP resources can be freed.
            task = asyncio.create_task(self._delete_thread(thread_id, token))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(task)
                except Exception:
                    _LOG.warning(
                        "Could not delete temporary cli-agent thread after cancellation",
                        exc_info=True,
                    )
                raise

    async def _delete_thread(self, thread_id: str, token: str) -> None:
        url = self.runtime.endpoint.removesuffix("/ag-ui") + "/threads/" + thread_id
        try:
            async with self._client_factory(timeout=8.0) as client:
                response = await client.delete(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
                if response.status_code not in {204, 404}:
                    response.raise_for_status()
        except Exception:
            _LOG.exception("Failed to delete temporary cli-agent thread %s", thread_id)
            raise

    @staticmethod
    async def _read_answer(response: httpx.Response, *, stop_event=None) -> str:
        chunks: dict[str, str] = {}
        finished_messages: list[str] = []
        completed = False
        lines: list[str] = []

        def consume() -> None:
            nonlocal completed
            if not lines:
                return
            event = json.loads("\n".join(lines))
            lines.clear()
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Invalid AG-UI response event")
            kind = event["type"]
            if kind == "TEXT_MESSAGE_START" and event.get("role") == "assistant":
                message_id = event.get("messageId")
                if not isinstance(message_id, str):
                    raise ValueError("Invalid AG-UI assistant message ID")
                chunks[message_id] = ""
            elif kind == "TEXT_MESSAGE_CONTENT":
                message_id = event.get("messageId")
                if message_id in chunks:
                    chunks[message_id] += str(event.get("delta") or "")
            elif kind == "TEXT_MESSAGE_END":
                message_id = event.get("messageId")
                if message_id in chunks:
                    finished_messages.append(chunks.pop(message_id))
            elif kind == "RUN_ERROR":
                raise RuntimeError("cli-agent reported an AG-UI run error")
            elif kind == "RUN_FINISHED":
                outcome = event.get("outcome") or {}
                if outcome.get("type") != "success":
                    raise RuntimeError(
                        "cli-agent run did not succeed; interactive approval "
                        "is not supported in a temporary Cmd session"
                    )
                if outcome.get("pendingToolCallIds"):
                    raise RuntimeError("cli-agent requires interactive frontend tools")
                completed = True

        async for line in response.aiter_lines():
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError()
            if completed:
                break
            if line == "":
                consume()
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))

        if lines and not completed:
            consume()
        if not completed:
            raise RuntimeError("cli-agent AG-UI stream ended before RUN_FINISHED")
        return "".join(finished_messages)
